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
    return _use_str_uuids(psycopg.connect(os.environ["DATABASE_URL"]))


def get_direct_connection() -> psycopg.Connection:
    return _use_str_uuids(psycopg.connect(os.environ["DIRECT_URL"]))
