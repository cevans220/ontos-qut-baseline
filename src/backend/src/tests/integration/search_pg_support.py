"""Shared setup for the Postgres search tests.

The tests run on throwaway schemas with ``search_path`` pinned to them, like
the app. An extension the app creates under that path lands in the throwaway
schema, and dropping the schema with CASCADE then drops the extension for the
whole database, along with anything depending on it (``lakebase_vector``
depends on ``vector``). On a shared Lakebase branch that breaks other tests and
sessions. So install the available search extensions into ``public`` first;
the app then finds them instead of creating its own.
"""
from sqlalchemy import text

SEARCH_EXTENSIONS = ("pg_trgm", "vector", "lakebase_text", "lakebase_vector")


def preinstall_search_extensions(conn) -> None:
    """Create every available search extension in ``public`` (best effort)."""
    available = {r[0] for r in conn.execute(text(
        "SELECT name FROM pg_available_extensions WHERE name = ANY(:n)"), {"n": list(SEARCH_EXTENSIONS)})}
    installed = {r[0] for r in conn.execute(text("SELECT extname FROM pg_extension"))}
    for ext in SEARCH_EXTENSIONS:
        if ext in available and ext not in installed:
            try:
                with conn.begin_nested():
                    conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext} SCHEMA public CASCADE"))
            except Exception:  # pragma: no cover - no privilege: the tests degrade like the app
                pass
