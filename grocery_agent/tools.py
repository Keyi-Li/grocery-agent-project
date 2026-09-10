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
from grocery_agent.embeddings import embed
from grocery_agent.repositories import (
    ActionLogRepository,
    InventoryRepository,
    RecipeRepository,
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
    stale_after_days: int | None = None,
) -> Item:
    product = InventoryRepository(conn).get_or_create_product(name)
    item_kwargs = dict(
        household_id=household_id,
        product_id=product.id,
        quantity=quantity,
        purchase_date=purchase_date or date.today(),
        expiry_date=expiry_date,
    )
    if stale_after_days is not None:
        item_kwargs["stale_after_days"] = stale_after_days
    item = Item(**item_kwargs)  # runs __post_init__ validation (stale_after_days >= 0)
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
            "stale_after_days": item.stale_after_days,
        },
    )
    return item


def update_item(
    conn: psycopg.Connection,
    household_id: str,
    user_id: str,
    name: str,
    purchase_date: date | None = None,
    expiry_date: date | None = None,
    stale_after_days: int | None = None,
) -> Item | None:
    """Corrects one or more fields already recorded for an item — a real
    write, for when the user's intent is clearly a correction, not a
    hypothetical (a genuine what-if question changes nothing, so it
    doesn't belong here — that's a custom_action case instead). Applies
    to whichever purchase of `name` is most recently on record, since
    that's the one a casual correction almost always means; ambiguous
    with multiple simultaneous batches of the same item. Returns None
    if nothing is on record to correct.

    If purchase_date is corrected without an explicit new expiry_date,
    the existing expiry_date (if any) shifts by the same number of days
    rather than being left stale — e.g. correcting the purchase date to
    3 days earlier moves a 7-day shelf life's expiry 3 days earlier too.
    This is deliberately computed here rather than left to the model,
    since it's exact arithmetic a cheap model shouldn't be trusted with.
    """
    inventory_repo = InventoryRepository(conn)
    product = inventory_repo.get_or_create_product(name)
    items = inventory_repo.get_items(household_id, product.id)
    if not items:
        return None
    item = max(items, key=lambda i: i.purchase_date)

    if expiry_date is not None:
        item.expiry_date = expiry_date
    elif purchase_date is not None and item.expiry_date is not None:
        item.expiry_date = item.expiry_date + (purchase_date - item.purchase_date)
    if purchase_date is not None:
        item.purchase_date = purchase_date
    if stale_after_days is not None:
        item.stale_after_days = stale_after_days

    inventory_repo.update_item(item)
    ActionLogRepository(conn).log(
        household_id,
        user_id,
        "update_item",
        {
            "name": name,
            "purchase_date": item.purchase_date.isoformat(),
            "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None,
            "stale_after_days": item.stale_after_days,
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
    original_quantities = {item.id: item.quantity for item in items}
    consume_from_items(items, quantity)
    for item in items:
        if item.quantity == original_quantities[item.id]:
            continue  # untouched by this consumption — nothing to persist
        if item.quantity <= 0:
            inventory_repo.delete_item(item.id)
        else:
            inventory_repo.update_item(item)

    new_entry = maybe_add_to_shopping_list(
        household_id, product.id, items, list_repo.get_entries(household_id)
    )
    if new_entry is not None:
        list_repo.add_entry(new_entry)
        ActionLogRepository(conn).log(
            household_id,
            user_id,
            "auto_add_to_shopping_list",
            {"name": name, "reason": "quantity reached zero"},
        )

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


def remove_from_shopping_list(
    conn: psycopg.Connection, household_id: str, user_id: str, name: str
) -> bool:
    """Returns whether an entry was actually removed, so callers (and
    custom_action-generated code looping over the list to clear it)
    can tell a no-op from a real removal."""
    inventory_repo = InventoryRepository(conn)
    list_repo = ShoppingListRepository(conn)

    product = inventory_repo.get_or_create_product(name)
    existing = next(
        (e for e in list_repo.get_entries(household_id) if e.product_id == product.id),
        None,
    )
    if existing is None:
        return False
    list_repo.remove_entry(existing.id)

    ActionLogRepository(conn).log(
        household_id, user_id, "remove_from_shopping_list", {"name": name}
    )
    return True


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
    """Per-item detail for one product — every user-meaningful field
    (quantity, purchase date, expiry date, staleness threshold) — for
    questions query_stock can't answer since it only returns an
    aggregate total (e.g. "when did I buy the milk?"). Internal ids are
    deliberately excluded, same as every other query tool — they mean
    nothing to the LLM or the person reading the reply."""
    inventory_repo = InventoryRepository(conn)
    product = inventory_repo.get_or_create_product(name)
    return [
        {
            "quantity": i.quantity,
            "purchase_date": i.purchase_date,
            "expiry_date": i.expiry_date,
            "stale_after_days": i.stale_after_days,
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


# Cosine-distance cutoff for suggest_recipes — below ~0.24 in practice
# for genuinely relevant matches, above ~0.37 for clearly unrelated
# ones (checked empirically against the real corpus, see the recipe
# feature's design discussion). Excluding weak matches rather than
# always returning k results regardless of relevance means an
# unmatchable stock correctly comes back empty instead of forcing a
# loose "sort of related" suggestion. Retune after more real usage.
RECIPE_MATCH_MAX_DISTANCE = 0.3


def suggest_recipes(
    conn: psycopg.Connection, household_id: str, preference: str | None = None
) -> list[dict]:
    """Retrieval-augmented recipe suggestion: builds a query from the
    household's actual current stock (+ an optional stated preference),
    embeds it, and retrieves the k most similar real recipes from the
    corpus (RecipeRepository, populated by scripts/ingest_recipes.py) —
    below RECIPE_MATCH_MAX_DISTANCE only, so a stock nothing actually
    matches comes back empty rather than forcing a loose suggestion.
    The LLM never invents a recipe here — only these real, retrieved
    ones ever reach it (see llm.generate_reply)."""
    in_stock = query_stock(conn, household_id)
    query_text = "Available ingredients: " + ", ".join(item["name"] for item in in_stock) + "."
    if preference:
        query_text += f" Preference: {preference}."

    recipes = RecipeRepository(conn).search(
        embed(query_text), k=3, max_distance=RECIPE_MATCH_MAX_DISTANCE
    )
    return [
        {"name": r.name, "ingredients": r.ingredients, "steps": r.steps} for r in recipes
    ]
