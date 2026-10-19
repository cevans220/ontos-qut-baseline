"""Labeled search evaluation (#1061): catalog, queries, recorded embeddings, metrics.

- ``search_eval_catalog.yaml``: search documents extracted from the retail demo
  data (``data/demo_data_retail.sql``).
- ``search_eval_queries.yaml``: labeled queries (exact names, plain language,
  zero-overlap paraphrases, typos) plus nonsense queries.
- ``search_eval_embeddings.json``: real ``databricks-gte-large-en`` vectors for
  every catalog document and query, int8-quantized. ``RecordedEmbedder`` replays
  them, so CI measures real semantic behaviour without calling an endpoint.

Re-record after changing the catalog, the queries or the embedding text:

    cd src/backend
    hatch -e dev run python -m src.tests.eval.search_eval record --profile <databricks-profile>
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import yaml

from src.common.search_embeddings import EmbeddingStats, embedding_text, text_hash
from src.common.search_interfaces import SearchIndexItem

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "search_eval_catalog.yaml"
QUERIES = HERE / "search_eval_queries.yaml"
EMBEDDINGS = HERE / "search_eval_embeddings.json"
MODEL = "databricks-gte-large-en"
DIM = 1024
CATEGORIES = ("exact", "plain", "paraphrase", "typo")


def load_items() -> List[SearchIndexItem]:
    data = yaml.safe_load(CATALOG.read_text())
    return [
        SearchIndexItem(
            id=it["id"], type=it["type"], feature_id=it["feature_id"], title=it["title"],
            description=it.get("description") or "", link=f"/eval/{it['id']}",
            tags=it.get("tags") or [], extra_data=it.get("extra_data") or {},
        )
        for it in data["items"]
    ]


@dataclass
class LabeledQuery:
    q: str
    category: str
    expected: List[str]


def load_queries() -> tuple[List[LabeledQuery], List[str]]:
    data = yaml.safe_load(QUERIES.read_text())
    return [LabeledQuery(**q) for q in data["queries"]], list(data.get("nonsense") or [])


def document_text(item: SearchIndexItem) -> str:
    """What the embedding worker embeds for ``item``."""
    from src.repositories.search_repository import _aux_text
    return embedding_text(item.title, item.description, _aux_text(item))


def query_text(query: str) -> str:
    """What ``QueryEmbeddingCache`` embeds for ``query``."""
    return " ".join(query.lower().split())


# --- recorded embeddings -----------------------------------------------------

# int8 per component with one scale per vector, hex-encoded: lowercase hex can't
# form the token shapes secret scanners look for (base64 of random bytes can).
def _encode(vec: Sequence[float]) -> str:
    scale = max(abs(x) for x in vec) or 1.0
    q = bytes((round(x / scale * 127) & 0xFF) for x in vec)
    return f"{scale!r}:{q.hex()}"


def _decode(s: str) -> List[float]:
    scale, hexed = s.split(":", 1)
    raw = bytes.fromhex(hexed)
    return [(b - 256 if b > 127 else b) / 127 * float(scale) for b in raw]


class RecordedEmbedder:
    """Replays recorded vectors by text hash; unknown text means the recording
    is out of date (re-record, see the module docstring)."""

    endpoint = MODEL
    dim = DIM

    def __init__(self, path: Path = EMBEDDINGS):
        self._vectors: Dict[str, str] = json.loads(path.read_text())["vectors"]
        self.stats = EmbeddingStats()

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        out = []
        for t in texts:
            key = text_hash(t, MODEL)
            if key not in self._vectors:
                raise KeyError(f"No recorded embedding for {t[:60]!r}; re-record search_eval_embeddings.json")
            out.append(_decode(self._vectors[key]))
        self.stats.add(calls=1, texts=len(texts))
        return out


def record(profile: str, batch_size: int = 32) -> int:
    """Embed every catalog document and query with the real endpoint and write
    ``search_eval_embeddings.json``. Returns the number of vectors."""
    from types import SimpleNamespace

    from databricks.sdk.core import Config

    from src.common.search_embeddings import DatabricksEmbedder

    cfg = Config(profile=profile)
    settings = SimpleNamespace(DATABRICKS_TOKEN=None, DATABRICKS_CONFIG_PROFILE=profile,
                               DATABRICKS_HOST=cfg.host, LLM_BASE_URL=None)
    embedder = DatabricksEmbedder(settings, MODEL, DIM)
    queries, nonsense = load_queries()
    texts = sorted({document_text(i) for i in load_items()}
                   | {query_text(q.q) for q in queries} | {query_text(q) for q in nonsense})
    vectors: Dict[str, str] = {}
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        for t, v in zip(batch, embedder.embed(batch)):
            vectors[text_hash(t, MODEL)] = _encode(v)
    EMBEDDINGS.write_text(json.dumps({"model": MODEL, "dim": DIM, "vectors": vectors}, sort_keys=True))
    return len(vectors)


# --- metrics ---------------------------------------------------------------

@dataclass
class ModeResult:
    mode: str
    recall_at_5: float = 0.0
    mrr: float = 0.0
    per_category: Dict[str, Dict[str, float]] = field(default_factory=dict)
    misses: List[str] = field(default_factory=list)

    def row(self) -> str:
        cats = "  ".join(f"{c}={self.per_category[c]['recall_at_5']:.2f}" for c in CATEGORIES
                         if c in self.per_category)
        return f"{self.mode:<9} recall@5={self.recall_at_5:.3f}  MRR={self.mrr:.3f}  ({cats})"


def recall_at_k(ranked: Sequence[str], expected: Sequence[str], k: int = 5) -> float:
    return len(set(ranked[:k]) & set(expected)) / min(len(expected), k)


def reciprocal_rank(ranked: Sequence[str], expected: Iterable[str]) -> float:
    exp = set(expected)
    for i, doc in enumerate(ranked, 1):
        if doc in exp:
            return 1.0 / i
    return 0.0


def evaluate(mode: str, queries: Sequence[LabeledQuery], rank) -> ModeResult:
    """``rank(query) -> [doc id]`` best first."""
    res = ModeResult(mode)
    by_cat: Dict[str, List[tuple[float, float]]] = {}
    for q in queries:
        ranked = rank(q.q)
        r5, rr = recall_at_k(ranked, q.expected), reciprocal_rank(ranked, q.expected)
        by_cat.setdefault(q.category, []).append((r5, rr))
        if not r5:
            res.misses.append(f"{q.category}: {q.q!r} -> {ranked[:3]}")
    allv = [v for vs in by_cat.values() for v in vs]
    res.recall_at_5 = sum(v[0] for v in allv) / len(allv)
    res.mrr = sum(v[1] for v in allv) / len(allv)
    res.per_category = {
        c: {"recall_at_5": sum(v[0] for v in vs) / len(vs), "mrr": sum(v[1] for v in vs) / len(vs)}
        for c, vs in by_cat.items()
    }
    return res


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> None:  # pragma: no cover - manual tool
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record real embeddings for the catalog and queries")
    rec.add_argument("--profile", required=True, help="Databricks CLI profile")
    args = ap.parse_args()
    if args.cmd == "record":
        print(f"Recorded {record(args.profile)} vectors to {EMBEDDINGS}")


if __name__ == "__main__":  # pragma: no cover
    main()
