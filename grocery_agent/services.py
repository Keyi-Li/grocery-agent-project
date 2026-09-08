"""Core business logic.

Pure functions operating on domain objects (grocery_agent.dataclass)
held in memory — no database, no I/O.
"""

from __future__ import annotations

from datetime import date

from grocery_agent.dataclass import Item, ShoppingListEntry

# Expiry reminders fire once an item comes within this many days of
# its expiry_date (or has already passed it).
EXPIRY_THRESHOLD_DAYS = 2


def consume_from_items(items: list[Item], quantity: float) -> None:
    """Deduct `quantity` from `items` in FEFO order, in place.

    Items with a known expiry_date are consumed soonest-first; items
    with no expiry_date are treated as expiring last. If `quantity`
    exceeds total stock, floors at zero rather than raising — over-
    reporting consumption ("I used 6" when 5 were on record) just
    means "none left," not a user error.
    """
    if quantity < 0:
        raise ValueError(f"quantity cannot be negative: {quantity}")

    ordered = sorted(items, key=lambda i: (i.expiry_date is None, i.expiry_date))

    remaining = quantity
    for item in ordered:
        if remaining <= 0:
            break
        deduct = min(item.quantity, remaining)
        item.quantity -= deduct
        remaining -= deduct


def is_expiring_soon(
    item: Item, today: date, threshold_days: int = EXPIRY_THRESHOLD_DAYS
) -> bool:
    """True once `item` is within `threshold_days` of expiring, or has
    already passed its expiry_date. False for items with no
    expiry_date."""
    if item.expiry_date is None:
        return False
    return (item.expiry_date - today).days <= threshold_days


def is_stale(item: Item, today: date) -> bool:
    """True once `item` has gone unconsumed for its own
    `stale_after_days` since purchase_date. Always False if staleness
    reminders are disabled (`stale_after_days == 0`) or the item has
    already been fully consumed."""
    if item.stale_after_days <= 0:
        return False
    if item.quantity <= 0:
        return False
    return (today - item.purchase_date).days >= item.stale_after_days


def maybe_add_to_shopping_list(
    household_id: str,
    product_id: str,
    items: list[Item],
    existing_entries: list[ShoppingListEntry],
) -> ShoppingListEntry | None:
    """Return a new auto `ShoppingListEntry` if `product_id`'s total
    quantity across `items` has hit 0 and no entry (auto or manual)
    for it already exists in `existing_entries`; otherwise return
    None. Does not mutate `existing_entries` — the caller persists
    the result."""
    total = sum(i.quantity for i in items if i.product_id == product_id)
    if total > 0:
        return None
    if any(e.product_id == product_id for e in existing_entries):
        return None
    return ShoppingListEntry(
        household_id=household_id, product_id=product_id, source="auto"
    )
