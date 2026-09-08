"""Tool functions — thin wrappers matching the LLM tool signatures
declared in grocery_agent.llm. Each calls into services.py and
repositories.py, then writes an ActionLog row. No LLM involved — these
are plain callable functions; grocery_agent.llm wires an LLM to fill
their arguments.
"""

from __future__ import annotations

from datetime import date

import psycopg

from grocery_agent.dataclass import Item, ShoppingListEntry
from grocery_agent.repositories import (
    ActionLogRepository,
    InventoryRepository,
    ShoppingListRepository,
)
from grocery_agent.services import (
    consume_from_items,
    is_expiring_soon,
    maybe_add_to_shopping_list,
)


def add_item(
    conn: psycopg.Connection,
    household_id: str,
    user_id: str,
    name: str,
    quantity: float,
    purchase_date: date | None = None,
    expiry_date: date | None = None,
) -> Item:
    product = InventoryRepository(conn).get_or_create_product(name)
    item = Item(
        household_id=household_id,
        product_id=product.id,
        quantity=quantity,
        purchase_date=purchase_date or date.today(),
        expiry_date=expiry_date,
    )
    InventoryRepository(conn).add_item(item)
    ActionLogRepository(conn).log(
        household_id,
        user_id,
        "add_item",
        {
            "name": name,
            "quantity": quantity,
            "purchase_date": item.purchase_date.isoformat(),
            "expiry_date": expiry_date.isoformat() if expiry_date else None,
        },
    )
    return item


def consume_item(
    conn: psycopg.Connection,
    household_id: str,
    user_id: str,
    name: str,
    quantity: float,
) -> list[Item]:
    inventory_repo = InventoryRepository(conn)
    list_repo = ShoppingListRepository(conn)

    product = inventory_repo.get_or_create_product(name)
    items = inventory_repo.get_items(household_id, product.id)
    consume_from_items(items, quantity)
    for item in items:
        if item.quantity <= 0:
            # Fully consumed — delete the row outright rather than
            # leaving a zero-quantity one around and hiding it at query
            # time. reminder_state rows tied to it cascade-delete
            # automatically (FK ON DELETE CASCADE).
            inventory_repo.delete_item(item.id)
        else:
            inventory_repo.save_item_quantity(item)

    new_entry = maybe_add_to_shopping_list(
        household_id, product.id, items, list_repo.get_entries(household_id)
    )
    if new_entry is not None:
        list_repo.add_entry(new_entry)

    ActionLogRepository(conn).log(
        household_id, user_id, "consume_item", {"name": name, "quantity": quantity}
    )
    return items


def add_to_shopping_list(
    conn: psycopg.Connection, household_id: str, user_id: str, name: str
) -> ShoppingListEntry:
    inventory_repo = InventoryRepository(conn)
    list_repo = ShoppingListRepository(conn)

    product = inventory_repo.get_or_create_product(name)
    existing = next(
        (e for e in list_repo.get_entries(household_id) if e.product_id == product.id),
        None,
    )
    entry = existing or ShoppingListEntry(
        household_id=household_id, product_id=product.id, source="manual"
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
    products = (
        [inventory_repo.get_or_create_product(name)]
        if name is not None
        else inventory_repo.get_all_products_for_household(household_id)
    )
    result = []
    for product in products:
        total = sum(
            i.quantity for i in inventory_repo.get_items(household_id, product.id)
        )
        if total <= 0:
            continue  # fully consumed — omit rather than show "0 eggs"
        result.append({"name": product.name, "quantity": total})
    return result


def query_item_details(
    conn: psycopg.Connection, household_id: str, name: str
) -> list[dict]:
    """Per-item detail for one product — purchase date, expiry date,
    quantity — for questions query_stock can't answer since it only
    returns an aggregate total (e.g. "when did I buy the milk?")."""
    inventory_repo = InventoryRepository(conn)
    product = inventory_repo.get_or_create_product(name)
    return [
        {
            "quantity": i.quantity,
            "purchase_date": i.purchase_date,
            "expiry_date": i.expiry_date,
        }
        for i in inventory_repo.get_items(household_id, product.id)
        if i.quantity > 0
    ]


def query_shopping_list(
    conn: psycopg.Connection, household_id: str
) -> list[ShoppingListEntry]:
    return ShoppingListRepository(conn).get_entries(household_id)


def query_expiring_soon(conn: psycopg.Connection, household_id: str) -> list[dict]:
    inventory_repo = InventoryRepository(conn)
    today = date.today()
    result = []
    for product in inventory_repo.get_all_products_for_household(household_id):
        for item in inventory_repo.get_items(household_id, product.id):
            if item.quantity > 0 and is_expiring_soon(item, today):
                result.append(
                    {
                        "name": product.name,
                        "quantity": item.quantity,
                        "expiry_date": item.expiry_date,
                    }
                )
    return result
