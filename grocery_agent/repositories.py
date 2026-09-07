"""Stage 3 persistence layer — repository classes wrapping Postgres.

Translate between Stage-1 domain objects (grocery_agent.dataclass) and
DB rows. See docs/grocery-agent-plan.md Section 3 and Stage 3.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Json

from grocery_agent.dataclass import Batch, Household, Item, ShoppingListEntry, User


class UserRepository:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def create(self, user: User) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email) VALUES (%s, %s)",
                (user.id, user.email),
            )

    def get(self, user_id: str) -> User | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, email FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return User(id=row[0], email=row[1]) if row else None


class HouseholdRepository:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def create(self, household: Household) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO households (id, name, telegram_chat_id) "
                "VALUES (%s, %s, %s)",
                (household.id, household.name, household.telegram_chat_id),
            )
            for user_id in household.member_ids:
                cur.execute(
                    "INSERT INTO household_members (household_id, user_id) "
                    "VALUES (%s, %s)",
                    (household.id, user_id),
                )

    def get(self, household_id: str) -> Household | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, telegram_chat_id FROM households WHERE id = %s",
                (household_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT user_id FROM household_members WHERE household_id = %s",
                (household_id,),
            )
            member_ids = [r[0] for r in cur.fetchall()]
        return Household(
            id=row[0], name=row[1], telegram_chat_id=row[2], member_ids=member_ids
        )

    def add_member(self, household_id: str, user_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO household_members (household_id, user_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (household_id, user_id),
            )

    def set_telegram_chat_id(self, household_id: str, chat_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE households SET telegram_chat_id = %s WHERE id = %s",
                (chat_id, household_id),
            )

    def get_by_telegram_chat_id(self, chat_id: str) -> Household | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM households WHERE telegram_chat_id = %s", (str(chat_id),)
            )
            row = cur.fetchone()
        return self.get(row[0]) if row else None


class InventoryRepository:
    """Wraps both `items` (the global canonical catalog) and `batches`
    (per-household stock) — used together for every stock operation."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def get_or_create_item(self, name: str, stale_after_days: int = 5) -> Item:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, stale_after_days FROM items WHERE name = %s",
                (name,),
            )
            row = cur.fetchone()
            if row is not None:
                return Item(id=row[0], name=row[1], stale_after_days=row[2])
            item = Item(name=name, stale_after_days=stale_after_days)
            cur.execute(
                "INSERT INTO items (id, name, stale_after_days) VALUES (%s, %s, %s)",
                (item.id, item.name, item.stale_after_days),
            )
        return item

    def get_item(self, item_id: str) -> Item | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, stale_after_days FROM items WHERE id = %s",
                (item_id,),
            )
            row = cur.fetchone()
        return Item(id=row[0], name=row[1], stale_after_days=row[2]) if row else None

    def add_batch(self, batch: Batch) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO batches (id, household_id, item_id, quantity, unit, "
                "purchase_date, expiry_date) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    batch.id,
                    batch.household_id,
                    batch.item_id,
                    batch.quantity,
                    batch.unit,
                    batch.purchase_date,
                    batch.expiry_date,
                ),
            )

    def get_batches(
        self, household_id: str, item_id: str | None = None
    ) -> list[Batch]:
        query = (
            "SELECT id, household_id, item_id, quantity, unit, purchase_date, "
            "expiry_date FROM batches WHERE household_id = %s"
        )
        params: list[str] = [household_id]
        if item_id is not None:
            query += " AND item_id = %s"
            params.append(item_id)
        query += " ORDER BY expiry_date NULLS LAST"
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [
            Batch(
                id=r[0],
                household_id=r[1],
                item_id=r[2],
                quantity=r[3],
                unit=r[4],
                purchase_date=r[5],
                expiry_date=r[6],
            )
            for r in rows
        ]

    def save_batch_quantity(self, batch: Batch) -> None:
        """Persist a batch's current in-memory quantity (e.g. after
        services.consume_from_batches mutated it) back to its row."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE batches SET quantity = %s WHERE id = %s",
                (batch.quantity, batch.id),
            )

    def get_all_items_for_household(self, household_id: str) -> list[Item]:
        """Every item that has at least one batch (of any quantity) in
        this household — used by tool functions to enumerate stock
        without the caller naming an item."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT items.id, items.name, items.stale_after_days "
                "FROM items JOIN batches ON batches.item_id = items.id "
                "WHERE batches.household_id = %s",
                (household_id,),
            )
            rows = cur.fetchall()
        return [Item(id=r[0], name=r[1], stale_after_days=r[2]) for r in rows]


class ShoppingListRepository:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def get_entries(self, household_id: str) -> list[ShoppingListEntry]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, household_id, item_id, source, created_at "
                "FROM shopping_list_entries WHERE household_id = %s",
                (household_id,),
            )
            rows = cur.fetchall()
        return [
            ShoppingListEntry(
                id=r[0], household_id=r[1], item_id=r[2], source=r[3], created_at=r[4]
            )
            for r in rows
        ]

    def add_entry(self, entry: ShoppingListEntry) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shopping_list_entries (id, household_id, item_id, "
                "source, created_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    entry.id,
                    entry.household_id,
                    entry.item_id,
                    entry.source,
                    entry.created_at,
                ),
            )

    def remove_entry(self, entry_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shopping_list_entries WHERE id = %s", (entry_id,)
            )


class ReminderStateRepository:
    """Tracks whether a reminder has already fired for a batch (plan
    Section 3b) — one-time for expiry, repeating for staleness."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def get(self, batch_id: str, reminder_type: str) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, batch_id, reminder_type, last_sent_at, active "
                "FROM reminder_state WHERE batch_id = %s AND reminder_type = %s",
                (batch_id, reminder_type),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "batch_id": row[1],
            "reminder_type": row[2],
            "last_sent_at": row[3],
            "active": row[4],
        }

    def upsert(
        self, batch_id: str, reminder_type: str, last_sent_at: datetime, active: bool = True
    ) -> None:
        existing = self.get(batch_id, reminder_type)
        with self._conn.cursor() as cur:
            if existing is None:
                cur.execute(
                    "INSERT INTO reminder_state (id, batch_id, reminder_type, "
                    "last_sent_at, active) VALUES (%s, %s, %s, %s, %s)",
                    (str(uuid.uuid4()), batch_id, reminder_type, last_sent_at, active),
                )
            else:
                cur.execute(
                    "UPDATE reminder_state SET last_sent_at = %s, active = %s "
                    "WHERE id = %s",
                    (last_sent_at, active, existing["id"]),
                )

    def deactivate_for_batch(self, batch_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE reminder_state SET active = false WHERE batch_id = %s",
                (batch_id,),
            )


class ActionLogRepository:
    """Append-only audit trail (plan Section 3d) — written by every
    Stage 4 tool function, never read back by the agent itself."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def log(self, household_id: str, user_id: str, action: str, details: dict) -> str:
        log_id = str(uuid.uuid4())
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO action_log (id, household_id, user_id, action, "
                "details, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    log_id,
                    household_id,
                    user_id,
                    action,
                    Json(details),
                    datetime.now(timezone.utc),
                ),
            )
        return log_id

    def get_for_household(self, household_id: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, household_id, user_id, action, details, created_at "
                "FROM action_log WHERE household_id = %s ORDER BY created_at",
                (household_id,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "household_id": r[1],
                "user_id": r[2],
                "action": r[3],
                "details": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]
