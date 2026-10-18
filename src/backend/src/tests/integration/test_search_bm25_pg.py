"""BM25 ranked list on the lakebase engine (#1059).

- ``SEARCH_TEST_PG_URL`` (local Postgres / CI): per-field tsvector columns exist,
  and the pg engine has no BM25 list.
- ``SEARCH_TEST_LAKEBASE_URL`` (a Lakebase branch with Lakebase Search enabled;
  manual / nightly): engine detection + BM25 indexes, ranking, Settings boosts,
  filters, unchanged keyword results, and the stale-statistics refresh.

Each connection pins ``search_path`` to a throwaway schema like the app, so the
extension objects (installed in ``public``) are only reachable schema-qualified.
"""
import copy
import os

import pytest
from sqlalchemy import text

from src.common.search_config_loader import SearchConfigLoader
from src.common.search_interfaces import SearchIndexItem
from src.controller.search_backend import PostgresBackend
from src.repositories import search_repository as repo
from src.repositories.search_engine import BM25_INDEXES, prepare_engine, refresh_bm25_statistics
from src.tests.integration.test_search_engine_pg import _schema_env
from src.tests.integration.test_search_settings_parity_pg import _YAML, ITEMS

_PG_URL = os.environ.get("SEARCH_TEST_PG_URL")
_LAKEBASE_URL = os.environ.get("SEARCH_TEST_LAKEBASE_URL")

needs_lakebase = pytest.mark.skipif(
    not _LAKEBASE_URL, reason="SEARCH_TEST_LAKEBASE_URL not set (Lakebase Search branch)"
)


def _config():
    return SearchConfigLoader(config_path=_YAML).load(force_reload=True)


@pytest.fixture()
def pg():
    if not _PG_URL:
        pytest.skip("SEARCH_TEST_PG_URL not set")
    factory, teardown = _schema_env(_PG_URL)
    try:
        yield factory
    finally:
        teardown()


@pytest.fixture()
def lakebase():
    factory, teardown = _schema_env(_LAKEBASE_URL)
    try:
        engine = prepare_engine(factory, "auto")
        with factory() as db:
            repo.upsert_documents(db, ITEMS)
        refresh_bm25_statistics(factory, engine)  # indexes were built on an empty table
        yield factory, engine
    finally:
        teardown()


def _ids(ranked):
    return [i for i, _ in ranked]


# ------------------------------------------------------------- pg engine (CI)

def test_field_tsv_columns_exist_and_pg_has_no_bm25_list(pg):
    with pg() as db:
        cols = {r[0] for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'search_documents' AND table_schema = current_schema()"
        ))}
        repo.upsert_documents(db, ITEMS)
        tsv = db.execute(text("SELECT title_tsv::text FROM search_documents WHERE id = 'product::1'")).scalar()
    assert {"title_tsv", "description_tsv", "aux_tsv"} <= cols
    assert "churn" in tsv and "custom" in tsv  # english stems
    engine = prepare_engine(pg, "auto")
    assert engine.name == "pg" and not engine.has_bm25
    with pg() as db:
        assert repo.bm25_rank(db, "customer churn", config=_config(), engine=engine) == []


# ------------------------------------------------------- lakebase engine

@needs_lakebase
def test_lakebase_engine_builds_one_bm25_index_per_field(lakebase):
    factory, engine = lakebase
    assert engine.name == "lakebase" and engine.has_bm25
    with factory() as db:
        defs = {r[0]: r[1] for r in db.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
        ))}
    for col, name in BM25_INDEXES.items():
        assert name in defs and "lakebase_bm25" in defs[name] and col in defs[name]


@needs_lakebase
def test_bm25_ranks_by_relevance_with_stemming(lakebase):
    factory, engine = lakebase
    with factory() as db:
        churn = repo.bm25_rank(db, "customer churn", config=_config(), engine=engine)
        stemmed = repo.bm25_rank(db, "churning customers", config=_config(), engine=engine)
        prefix = repo.bm25_rank(db, "cust", config=_config(), engine=engine)
    assert churn[0][0] == "product::1"                 # both terms in the title
    assert all(s > 0 for _, s in churn)
    assert _ids(stemmed)[:2] == _ids(churn)[:2]        # stems match
    assert prefix == []                                # whole words only (keyword search keeps prefixes)


@needs_lakebase
def test_settings_boosts_change_the_bm25_order(lakebase):
    factory, engine = lakebase
    base = _config()
    desc_heavy = copy.deepcopy(base)
    desc_heavy.defaults.fields.title.boost = 0.1
    desc_heavy.defaults.fields.description.boost = 10.0
    for at in desc_heavy.asset_types.values():
        if "title" in at.fields:
            at.fields["title"].boost = 0.1
        if "description" in at.fields:
            at.fields["description"].boost = 10.0
    with factory() as db:
        by_title = repo.bm25_rank(db, "customer", config=base, engine=engine)
        by_desc = repo.bm25_rank(db, "customer", config=desc_heavy, engine=engine)
    # "Orders Contract" mentions customer only in its description.
    assert _ids(by_desc).index("contract::4") < _ids(by_title).index("contract::4")


@needs_lakebase
def test_bm25_applies_filters_permissions_and_disabled_types(lakebase):
    factory, engine = lakebase
    config = _config()
    with factory() as db:
        products = repo.bm25_rank(db, "customer", config=config, engine=engine, type_filter="data-product")
        allowed = repo.bm25_rank(db, "customer", config=config, engine=engine, allowed_features=["data-contracts"])
        config.asset_types["data-contract"].enabled = False
        no_contracts = repo.bm25_rank(db, "customer", config=config, engine=engine)
    assert products and all(i.startswith("product::") for i in _ids(products))
    assert allowed and all(i.startswith("contract::") for i in _ids(allowed))
    assert not any(i.startswith("contract::") for i in _ids(no_contracts))


@needs_lakebase
def test_keyword_results_are_unchanged_on_the_lakebase_engine(lakebase):
    """BM25 is only an extra signal until hybrid fusion (#1061)."""
    factory, engine = lakebase
    with factory() as db:
        for q in ("customer", "customer churn", "cust", "orders contract"):
            assert repo.search(db, q, config=_config(), engine=engine) == repo.search(db, q, config=_config())


@needs_lakebase
def test_bm25_statistics_are_stale_until_refreshed(lakebase):
    """BM25 corpus statistics are fixed at index build time: bulk-inserting more
    documents with the same term leaves existing scores unchanged until the
    indexes are rebuilt, after which the term's IDF (and the score) drops."""
    factory, engine = lakebase

    def score_of(doc_id):
        with factory() as db:
            return dict(repo.bm25_rank(db, "churn", config=_config(), engine=engine, limit=1000))[doc_id]

    before = score_of("product::1")
    filler = [
        SearchIndexItem(id=f"product::bulk{n}", type="data-product", feature_id="data-products",
                        title=f"Churn report {n}", description="churn churn", link="/", tags=[], extra_data={})
        for n in range(300)
    ]
    with factory() as db:
        repo.upsert_documents(db, filler)
    stale = score_of("product::1")
    timings = refresh_bm25_statistics(factory, engine)
    fresh = score_of("product::1")

    assert stale == pytest.approx(before, rel=1e-6)
    assert fresh < before
    assert set(timings) == set(BM25_INDEXES.values()) and all(t is not None for t in timings.values())


@needs_lakebase
def test_reconcile_refreshes_bm25_statistics(lakebase):
    factory, engine = lakebase
    backend = PostgresBackend(factory, config_provider=_config)
    backend.engine = engine
    stats = backend.reconcile([("All", lambda: ITEMS)], is_indexable=lambda it: bool(it.feature_id))
    assert stats["status"] == "done"
    assert set(stats["bm25_refresh"]) == set(BM25_INDEXES.values())
    assert backend.bm25_rank("customer churn")[0][0] == "product::1"
