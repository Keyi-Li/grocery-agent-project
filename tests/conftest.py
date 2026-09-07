"""Shared fixtures for persistence tests (Stage 3+).

Runs against the real Supabase project configured in .env: schema is
applied once per test session (idempotent CREATE TABLE IF NOT EXISTS),
and each test gets its own connection whose transaction is rolled back
afterward so tests don't leave data behind.
"""

from __future__ import annotations

import pathlib

import pytest

from grocery_agent.db import get_connection, get_direct_connection

SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "grocery_agent" / "schema.sql"


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    conn = get_direct_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_conn():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
