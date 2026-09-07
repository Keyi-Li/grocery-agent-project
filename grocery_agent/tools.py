"""Stage 4 tool functions — thin wrappers matching the LLM tool
signatures in docs/grocery-agent-plan.md Section 4. Each calls into
Stage 2 (services) and Stage 3 (repositories), then writes an
ActionLog row. No LLM involved — these are plain callable functions,
invoked here with fixed arguments; Stage 5 wires an LLM to fill them.
"""

from __future__ import annotations

from datetime import date

import psycopg

from grocery_agent.dataclass import Batch, ShoppingListEntry
from grocery_agent.repositories import (
    ActionLogRepository,
    InventoryRepository,
    ReminderStateRepository,
    ShoppingListRepository,
)
from grocery_agent.services import consume_from_batches, is_expiring_soon, maybe_add_to_shopping_list


def _summarize(batches: list[Batch]) -> tuple[float, str]:
    in_stock = [b for b in batches if b.quantity > 0]
    total = sum(b.quantity for b in in_stock)
    units = {b.unit for b in in_stock}
    unit = units.pop() if len(units) == 1 else ("mixed" if units else "unit")
    return total, unit


def add_item(
    conn: psycopg.Connection,
    household_id: str,
    user_id: str,
    name: str,
    quantity: float,
    unit: str = "unit",
    expiry_date: date | None = None,
) -> Batch:
    item = InventoryRepository(conn).get_or_create_item(name)
    batch = Batch(
        household_id=household_id,
        item_id=item.id,
        quantity=quantity,
        unit=unit,
        purchase_date=date.today(),
        expiry_date=expiry_date,
    )
    InventoryRepository(conn).add_batch(batch)
    ActionLogRepository(conn).log(
        household_id,
        user_id,
        "add_item",
        {
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "expiry_date": expiry_date.isoformat() if expiry_date else None,
        },
    )
    return batch


def consume_item(
    conn: psycopg.Connection,
    household_id: str,
    user_id: str,
    name: str,
    quantity: float,
) -> list[Batch]:
    inventory_repo = InventoryRepository(conn)
    list_repo = ShoppingListRepository(conn)

    item = inventory_repo.get_or_create_item(name)
    batches = inventory_repo.get_batches(household_id, item.id)
    consume_from_batches(batches, quantity)
    reminder_repo = ReminderStateRepository(conn)
    for batch in batches:
        inventory_repo.save_batch_quantity(batch)
        if batch.quantity == 0:
            # Plan Section 3: consuming a batch to 0 deactivates any
            # reminders tied to it, so it stops nagging about a batch
            # that's gone.
            reminder_repo.deactivate_for_batch(batch.id)

    new_entry = maybe_add_to_shopping_list(
        household_id, item.id, batches, list_repo.get_entries(household_id)
    )
    if new_entry is not None:
        list_repo.add_entry(new_entry)

    ActionLogRepository(conn).log(
        household_id, user_id, "consume_item", {"name": name, "quantity": quantity}
    )
    return batches


def add_to_shopping_list(
    conn: psycopg.Connection, household_id: str, user_id: str, name: str
) -> ShoppingListEntry:
    inventory_repo = InventoryRepository(conn)
    list_repo = ShoppingListRepository(conn)

    item = inventory_repo.get_or_create_item(name)
    existing = next(
        (e for e in list_repo.get_entries(household_id) if e.item_id == item.id), None
    )
    entry = existing or ShoppingListEntry(
        household_id=household_id, item_id=item.id, source="manual"
    )
    if existing is None:
        list_repo.add_entry(entry)

    ActionLogRepository(conn).log(
        household_id, user_id, "add_to_shopping_list", {"name": name}
    )
    return entry


def query_stock(
    conn: psycopg.Connection, household_id: str, name: str | None = None
) -> list[dict]:
    inventory_repo = InventoryRepository(conn)
    items = (
        [inventory_repo.get_or_create_item(name)]
        if name is not None
        else inventory_repo.get_all_items_for_household(household_id)
    )
    result = []
    for item in items:
        total, unit = _summarize(inventory_repo.get_batches(household_id, item.id))
        if total <= 0:
            continue  # fully consumed — omit rather than show "0 eggs"
        result.append({"name": item.name, "quantity": total, "unit": unit})
    return result


def query_batch_details(
    conn: psycopg.Connection, household_id: str, name: str
) -> list[dict]:
    """Per-batch detail for one item — purchase date, expiry date,
    quantity — for questions query_stock can't answer since it only
    returns an aggregate total (e.g. "when did I buy the milk?")."""
    inventory_repo = InventoryRepository(conn)
    item = inventory_repo.get_or_create_item(name)
    return [
        {
            "quantity": b.quantity,
            "unit": b.unit,
            "purchase_date": b.purchase_date,
            "expiry_date": b.expiry_date,
        }
        for b in inventory_repo.get_batches(household_id, item.id)
        if b.quantity > 0
    ]


def query_shopping_list(
    conn: psycopg.Connection, household_id: str
) -> list[ShoppingListEntry]:
    return ShoppingListRepository(conn).get_entries(household_id)


def query_expiring_soon(conn: psycopg.Connection, household_id: str) -> list[dict]:
    inventory_repo = InventoryRepository(conn)
    today = date.today()
    result = []
    for item in inventory_repo.get_all_items_for_household(household_id):
        for batch in inventory_repo.get_batches(household_id, item.id):
            if batch.quantity > 0 and is_expiring_soon(batch, today):
                result.append(
                    {
                        "name": item.name,
                        "quantity": batch.quantity,
                        "unit": batch.unit,
                        "expiry_date": batch.expiry_date,
                    }
                )
    return result
