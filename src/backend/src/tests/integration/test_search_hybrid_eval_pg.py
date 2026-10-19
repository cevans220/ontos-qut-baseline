"""Hybrid RRF search (#1061): labeled evaluation + fusion behaviour on real Postgres.

The catalog (retail demo entities) and labeled queries live in ``tests/eval``;
embeddings are real ``databricks-gte-large-en`` vectors recorded there, so CI
measures real semantic behaviour without an endpoint.

- ``SEARCH_TEST_PG_URL`` with pgvector (CI): ``pg`` engine (Settings ranking +
  pg_trgm + HNSW).
- ``SEARCH_TEST_LAKEBASE_URL`` (opt-in): the ``lakebase`` engine (adds BM25 and
  lakebase_ann); results are printed for the record.
"""
import os

import pytest
from sqlalchemy import text

from src.common.search_config_loader import SearchConfigLoader
from src.common.search_embeddings import QueryEmbeddingCache
from src.controller.search_backend import PostgresBackend
from src.controller.search_embedding_worker import EmbeddingWorker
from src.models.search_config import SearchMode
from src.repositories import search_repository as repo
from src.repositories.search_engine import prepare_engine
from src.tests.eval import search_eval as ev
from src.tests.integration.test_search_engine_pg import _schema_env

_PG_URL = os.environ.get("SEARCH_TEST_PG_URL")
_LAKEBASE_URL = os.environ.get("SEARCH_TEST_LAKEBASE_URL")
MODES = ("lexical", "semantic", "hybrid")
# Hybrid may trade a little MRR for recall against the best single method.
MRR_TOLERANCE = 0.05


def _load_env(url, requested):
    factory, teardown = _schema_env(url)
    with factory() as db:
        has_vector = db.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar()
    if not has_vector:
        teardown()
        pytest.skip("pgvector not available on this Postgres")
    engine = prepare_engine(factory, requested, embedding_dim=ev.DIM)
    if not engine.embedding_dim:
        teardown()
        pytest.skip("embedding column unavailable")
    with factory() as db:
        repo.upsert_documents(db, ev.load_items())
    embedder = ev.RecordedEmbedder()
    EmbeddingWorker(factory, engine, embedder, batch_size=64).run_until_idle()
    config = SearchConfigLoader().load(force_reload=True).model_copy(deep=True)  # the shipped Settings
    backend = PostgresBackend(factory, config_provider=lambda: config, embedder=embedder)
    backend.engine, backend._prepared = engine, True
    backend._query_vectors = QueryEmbeddingCache(embedder)
    return backend, config, teardown


@pytest.fixture(scope="module")
def pg_eval():
    if not _PG_URL:
        pytest.skip("SEARCH_TEST_PG_URL not set")
    backend, config, teardown = _load_env(_PG_URL, "pg")
    try:
        yield backend, config
    finally:
        teardown()


def _ranker(backend, mode):
    return lambda q: [it.id for it in backend.search_items(q, limit=20, mode=mode)]


def _report(backend, label, capsys):
    """Evaluate every mode and print the table to the test log, even without ``-s``."""
    queries, _ = ev.load_queries()
    results = {m: ev.evaluate(m, queries, _ranker(backend, m)) for m in MODES}
    with capsys.disabled():
        print(f"\n[{label}] {len(queries)} labeled queries")
        for m in MODES:
            print("  " + results[m].row())
        for miss in results["hybrid"].misses:
            print(f"  hybrid miss  {miss}")
    return results


# ------------------------------------------------------------ evaluation

def test_hybrid_beats_lexical_and_semantic(pg_eval, capsys):
    backend, _ = pg_eval
    r = _report(backend, "pg engine", capsys)
    assert r["hybrid"].recall_at_5 > r["lexical"].recall_at_5
    assert r["hybrid"].recall_at_5 > r["semantic"].recall_at_5
    assert r["hybrid"].mrr >= max(r["lexical"].mrr, r["semantic"].mrr) - MRR_TOLERANCE


def test_zero_overlap_paraphrases_resolve_in_hybrid(pg_eval):
    """Fusion keeps every paraphrase the embedding model resolves, and most
    resolve. Two in the set are beyond the model (rank 8 and 11 semantically)
    and are kept as honest hard cases."""
    backend, _ = pg_eval
    queries, _ = ev.load_queries()
    paraphrases = [q for q in queries if q.category == "paraphrase"]
    hybrid, semantic = _ranker(backend, "hybrid"), _ranker(backend, "semantic")
    resolved = {q.q for q in paraphrases if ev.recall_at_k(hybrid(q.q), q.expected)}
    by_model = {q.q for q in paraphrases if ev.recall_at_k(semantic(q.q), q.expected)}
    assert by_model <= resolved
    assert len(resolved) >= 0.8 * len(paraphrases)


def test_nonsense_queries_find_nothing(pg_eval):
    backend, _ = pg_eval
    _, nonsense = ev.load_queries()
    for q in nonsense:
        assert backend.search(q, mode="hybrid")["total_count"] == 0, q


# ------------------------------------------------------- fusion behaviour

def test_settings_changes_apply_on_the_next_request(pg_eval):
    backend, config = pg_eval
    target = "asset::dashboard-customer-lifetime-value-overview"
    assert target in _ranker(backend, None)("client attrition")[:5]   # auto = hybrid here
    saved = config.hybrid.model_copy()
    try:
        config.hybrid.vector_weight = 0.0                             # semantic list off
        assert target not in _ranker(backend, None)("client attrition")[:5]
        config.hybrid.vector_weight = saved.vector_weight
        config.hybrid.mode = SearchMode.LEXICAL                       # keyword search only
        assert backend.resolve_mode(config) == "lexical"
        assert target not in _ranker(backend, None)("client attrition")[:5]
        config.hybrid.mode = saved.mode
        config.hybrid.min_vector_similarity = 0.99                    # floor above every match
        assert target not in _ranker(backend, None)("client attrition")[:5]
    finally:
        config.hybrid = saved
    assert target in _ranker(backend, None)("client attrition")[:5]


def test_pagination_total_count_and_facets_are_exact(pg_eval):
    backend, _ = pg_eval
    query = "customer personal data protection rules"  # broad keyword match + semantic hits
    assert backend._query_vector(query) is not None       # recorded, so the semantic list takes part
    full = backend.search(query, mode="hybrid", limit=100)
    assert full["total_count"] <= 100 or full["has_more"]
    whole = [(r["type"], r["id"]) for r in full["results"]]
    paged, offset = [], 0
    while True:
        page = backend.search(query, mode="hybrid", limit=7, offset=offset, include_facets=False)
        assert page["total_count"] == full["total_count"]
        paged += [(r["type"], r["id"]) for r in page["results"]]
        if not page["has_more"]:
            break
        offset += 7
    assert len(paged) == full["total_count"] == len(set(paged))     # no gaps, no duplicates
    assert paged[:len(whole)] == whole                                # same order as one big page
    assert sum(full["facets"]["type"].values()) == full["total_count"]


def test_semantic_hits_without_keyword_match_are_counted_once(pg_eval):
    backend, _ = pg_eval
    lexical = backend.search("client attrition", mode="lexical")
    hybrid = backend.search("client attrition", mode="hybrid", limit=100)
    lexical_ids = {r["id"] for r in backend.search("client attrition", mode="lexical", limit=100)["results"]}
    extra = [r for r in hybrid["results"] if r["id"] not in lexical_ids]
    assert extra and hybrid["total_count"] == lexical["total_count"] + len(extra)


def test_filters_and_permissions_apply_to_the_fused_list(pg_eval):
    backend, _ = pg_eval
    res = backend.search("what a shopper is worth", mode="hybrid", type_filter="asset-business-term", limit=50)
    assert res["results"] and {r["type"] for r in res["results"]} == {"asset-business-term"}
    res = backend.search("what a shopper is worth", mode="hybrid", allowed_features=["teams"], limit=50)
    assert {r["type"] for r in res["results"]} <= {"team"}


def test_list_queries_are_unchanged(pg_eval):
    backend, _ = pg_eval
    assert backend.search("*", mode="hybrid", limit=5) == backend.search("*", mode="lexical", limit=5)


# ------------------------------------------------------- lakebase engine

@pytest.mark.skipif(not _LAKEBASE_URL, reason="SEARCH_TEST_LAKEBASE_URL not set (Lakebase Search branch)")
def test_lakebase_engine_evaluation(capsys):
    backend, config, teardown = _load_env(_LAKEBASE_URL, "auto")
    try:
        assert backend.engine.name == "lakebase"
        # The BM25 list (off by default) fuses with exact totals and paging too.
        config.hybrid.bm25_weight = 0.5
        query = "inventory stock levels by warehouse"
        full = backend.search(query, mode="hybrid", limit=100, include_facets=False)
        paged = [r["id"] for off in range(0, full["total_count"], 9)
                 for r in backend.search(query, mode="hybrid", limit=9, offset=off, include_facets=False)["results"]]
        assert len(paged) == len(set(paged)) == full["total_count"]
        config.hybrid.bm25_weight = 0.0
        r = _report(backend, "lakebase engine (lakebase_ann; BM25 at its default weight)", capsys)
        assert r["hybrid"].recall_at_5 > r["lexical"].recall_at_5
        assert r["hybrid"].recall_at_5 > r["semantic"].recall_at_5
    finally:
        teardown()
