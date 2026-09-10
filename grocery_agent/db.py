"""Database connection helpers for the persistence layer.

DATABASE_URL (transaction-mode pooler) is used for normal app queries;
DIRECT_URL (session-mode pooler) is used for schema/migrations.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from psycopg.types.string import TextLoader

load_dotenv()


def _use_str_uuids(conn: psycopg.Connection) -> psycopg.Connection:
    """All ids are plain `str` throughout the domain/dataclass layer —
    without this, native `uuid` columns come back as `uuid.UUID`
    objects, which don't compare equal to the strings we generate
    client-side (uuid.uuid4() -> str)."""
    conn.adapters.register_loader("uuid", TextLoader)
    return conn


def _register_vector_type(conn: psycopg.Connection) -> psycopg.Connection:
    """Lets recipes.embedding round-trip as a plain list[float] instead of
    a raw pgvector literal string. Skipped silently if the `vector`
    extension isn't installed yet (e.g. schema.sql hasn't been applied to
    this DB yet) — every other table works fine without it; only
    recipe search needs this."""
    try:
        register_vector(conn)
    except Exception:
        pass
    return conn


def get_connection() -> psycopg.Connection:
    # prepare_threshold=None disables psycopg's client-side auto-prepared
    # statements. DATABASE_URL goes through Supabase's transaction-mode
    # pooler, which can hand a logical connection different underlying
    # Postgres backends between transactions — a prepared-statement name
    # from one backend can then collide with a stale one left on
    # another ("DuplicatePreparedStatement"). Not needed on
    # get_direct_connection: DIRECT_URL is session-mode (one dedicated
    # backend per connection), so this can't happen there.
    conn = psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None)
    return _register_vector_type(_use_str_uuids(conn))


def get_direct_connection() -> psycopg.Connection:
    conn = psycopg.connect(os.environ["DIRECT_URL"])
    return _register_vector_type(_use_str_uuids(conn))
