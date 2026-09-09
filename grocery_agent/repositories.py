"""Persistence layer — repository classes wrapping Postgres.

Translates between domain objects (grocery_agent.dataclass) and DB
rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Json

from grocery_agent.dataclass import Household, Item, Product, ShoppingListEntry


class HouseholdRepository:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def create(self, household: Household) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO households (id, name, telegram_chat_id, timezone) "
                "VALUES (%s, %s, %s, %s)",
                (household.id, household.name, household.telegram_chat_id, household.timezone),
            )

    def get(self, household_id: str) -> Household | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, telegram_chat_id, timezone FROM households WHERE id = %s",
                (household_id,),
            )
            row = cur.fetchone()
        return Household(id=row[0], name=row[1], telegram_chat_id=row[2], timezone=row[3]) if row else None

    def get_by_telegram_chat_id(self, chat_id: str) -> Household | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, telegram_chat_id, timezone FROM households WHERE telegram_chat_id = %s",
                (str(chat_id),),
            )
            row = cur.fetchone()
        return Household(id=row[0], name=row[1], telegram_chat_id=row[2], timezone=row[3]) if row else None

    def get_all(self) -> list[Household]:
        """Every household on record — used by the scheduled reminder
        check, which has no single household to scope to."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name, telegram_chat_id, timezone FROM households")
            rows = cur.fetchall()
        return [
            Household(id=r[0], name=r[1], telegram_chat_id=r[2], timezone=r[3]) for r in rows
        ]


class InventoryRepository:
    """Wraps both `products` (the global canonical catalog) and `items`
    (per-household stock) — used together for every stock operation."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    # --- products (shared catalog: name <-> id) ---------------------

    def get_or_create_product(self, name: str) -> Product:
        """name -> id, creating the catalog row the first time an item
        is ever mentioned. Called wherever a tool receives a raw `name`
        string from the LLM."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name FROM products WHERE name = %s", (name,))
            row = cur.fetchone()
            if row is not None:
                return Product(id=row[0], name=row[1])
            product = Product(name=name)
            cur.execute(
                "INSERT INTO products (id, name) VALUES (%s, %s)",
                (product.id, product.name),
            )
        return product

    def get_product(self, product_id: str) -> Product | None:
        """id -> name, read-only. Called wherever code already has a
        product_id (from a fetched Item or ShoppingListEntry) and needs
        it back as a display name."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name FROM products WHERE id = %s", (product_id,))
            row = cur.fetchone()
        return Product(id=row[0], name=row[1]) if row else None

    def get_all_products_for_household(self, household_id: str) -> list[Product]:
        """Every product that has at least one item (of any quantity) in
        this household — used by tool functions to enumerate stock
        without the caller naming a product."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT products.id, products.name "
                "FROM products JOIN items ON items.product_id = products.id "
                "WHERE items.household_id = %s",
                (household_id,),
            )
            rows = cur.fetchall()
        return [Product(id=r[0], name=r[1]) for r in rows]

    # --- items (per-household stock: one row per purchase) ----------

    def add_item(self, item: Item) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO items (id, household_id, product_id, quantity, "
                "purchase_date, expiry_date, stale_after_days) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    item.id,
                    item.household_id,
                    item.product_id,
                    item.quantity,
                    item.purchase_date,
                    item.expiry_date,
                    item.stale_after_days,
                ),
            )

    def get_items(
        self, household_id: str, product_id: str | None = None
    ) -> list[Item]:
        query = (
            "SELECT id, household_id, product_id, quantity, purchase_date, "
            "expiry_date, stale_after_days FROM items WHERE household_id = %s"
        )
        params: list[str] = [household_id]
        if product_id is not None:
            query += " AND product_id = %s"
            params.append(product_id)
        query += " ORDER BY expiry_date NULLS LAST"
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [
            Item(
                id=r[0],
                household_id=r[1],
                product_id=r[2],
                quantity=r[3],
                purchase_date=r[4],
                expiry_date=r[5],
                stale_after_days=r[6],
            )
            for r in rows
        ]

    def update_item(self, item: Item) -> None:
        """Persist an item's current in-memory state — quantity (e.g.
        after services.consume_from_items mutated it), and/or a
        purchase_date/expiry_date/stale_after_days correction — back to
        its row. Always writes all four mutable columns"""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE items SET quantity = %s, purchase_date = %s, "
                "expiry_date = %s, stale_after_days = %s WHERE id = %s",
                (
                    item.quantity,
                    item.purchase_date,
                    item.expiry_date,
                    item.stale_after_days,
                    item.id,
                ),
            )

    def delete_item(self, item_id: str) -> None:
        """Removes a fully-consumed item row outright rather than leaving
        a zero-quantity one around. reminder_state rows tied to it
        cascade-delete automatically (FK ON DELETE CASCADE)."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM items WHERE id = %s", (item_id,))


class ShoppingListRepository:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def get_entries(self, household_id: str) -> list[ShoppingListEntry]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, household_id, product_id, source "
                "FROM shopping_list_entries WHERE household_id = %s",
                (household_id,),
            )
            rows = cur.fetchall()
        return [
            ShoppingListEntry(id=r[0], household_id=r[1], product_id=r[2], source=r[3])
            for r in rows
        ]

    def add_entry(self, entry: ShoppingListEntry) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shopping_list_entries (id, household_id, product_id, "
                "source) VALUES (%s, %s, %s, %s)",
                (entry.id, entry.household_id, entry.product_id, entry.source),
            )

    def remove_entry(self, entry_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shopping_list_entries WHERE id = %s", (entry_id,)
            )


class ReminderStateRepository:
    """Tracks whether a reminder has already fired for an item —
    one-time for expiry, repeating for staleness."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def get(self, item_id: str, reminder_type: str) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, item_id, reminder_type, last_sent_at, active "
                "FROM reminder_state WHERE item_id = %s AND reminder_type = %s",
                (item_id, reminder_type),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "item_id": row[1],
            "reminder_type": row[2],
            "last_sent_at": row[3],
            "active": row[4],
        }

    def update(
        self, item_id: str, reminder_type: str, last_sent_at: datetime, active: bool = True
    ) -> None:
        existing = self.get(item_id, reminder_type)
        with self._conn.cursor() as cur:
            if existing is None:
                cur.execute(
                    "INSERT INTO reminder_state (id, item_id, reminder_type, "
                    "last_sent_at, active) VALUES (%s, %s, %s, %s, %s)",
                    (str(uuid.uuid4()), item_id, reminder_type, last_sent_at, active),
                )
            else:
                cur.execute(
                    "UPDATE reminder_state SET last_sent_at = %s, active = %s "
                    "WHERE id = %s",
                    (last_sent_at, active, existing["id"]),
                )


class ActionLogRepository:
    """Append-only audit trail. `user_id` is a plain label (the
    Telegram sender's id/name), not a foreign key — there's no users
    table to reference."""

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
