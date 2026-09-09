"""Database connection helpers for the persistence layer.

DATABASE_URL (transaction-mode pooler) is used for normal app queries;
DIRECT_URL (session-mode pooler) is used for schema/migrations.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv
from psycopg.types.string import TextLoader

load_dotenv()


def _use_str_uuids(conn: psycopg.Connection) -> psycopg.Connection:
    """All ids are plain `str` throughout the domain/dataclass layer —
    without this, native `uuid` columns come back as `uuid.UUID`
    objects, which don't compare equal to the strings we generate
    client-side (uuid.uuid4() -> str)."""
    conn.adapters.register_loader("uuid", TextLoader)
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
    return _use_str_uuids(
        psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None)
    )


def get_direct_connection() -> psycopg.Connection:
    return _use_str_uuids(psycopg.connect(os.environ["DIRECT_URL"]))
