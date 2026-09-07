"""Database connection helpers for the persistence layer (Stage 3).

DATABASE_URL (transaction-mode pooler) is used for normal app queries;
DIRECT_URL (session-mode pooler) is used for schema/migrations. See
.env.example and docs/grocery-agent-plan.md Stage 3.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_connection() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def get_direct_connection() -> psycopg.Connection:
    return psycopg.connect(os.environ["DIRECT_URL"])
