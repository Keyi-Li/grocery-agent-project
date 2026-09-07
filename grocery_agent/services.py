"""Stage 2 core business logic / services.

Pure functions operating on Stage-1 domain objects (grocery_agent.dataclass)
held in memory — no database, no I/O. See docs/grocery-agent-plan.md,
Section 3 (consumption/shopping-list logic), Section 3b (reminders),
and Stage 2.
"""

from __future__ import annotations

from datetime import date

from grocery_agent.dataclass import Batch, Item, ShoppingListEntry

# Plan Section 3b: expiry reminders fire once a batch comes within this
# many days of its expiry_date (or has already passed it).
EXPIRY_THRESHOLD_DAYS = 2


def consume_from_batches(batches: list[Batch], quantity: float) -> None:
    """Deduct `quantity` from `batches` in FEFO order, in place.

    Batches with a known expiry_date are consumed soonest-first;
    batches with no expiry_date are treated as expiring last. If
    `quantity` exceeds total stock, floors at zero rather than raising
    — over-reporting consumption ("I used 6" when 5 were on record)
    just means "none left," not a user error.
    """
    if quantity < 0:
        raise ValueError(f"quantity cannot be negative: {quantity}")

    ordered = sorted(batches, key=lambda b: (b.expiry_date is None, b.expiry_date))

    remaining = quantity
    for batch in ordered:
        if remaining <= 0:
            break
        deduct = min(batch.quantity, remaining)
        batch.quantity -= deduct
        remaining -= deduct


def is_expiring_soon(
    batch: Batch, today: date, threshold_days: int = EXPIRY_THRESHOLD_DAYS
) -> bool:
    """True once `batch` is within `threshold_days` of expiring, or has
    already passed its expiry_date. False for batches with no
    expiry_date."""
    if batch.expiry_date is None:
        return False
    return (batch.expiry_date - today).days <= threshold_days


def is_stale(batch: Batch, item: Item, today: date) -> bool:
    """True once `batch` has gone unconsumed for `item.stale_after_days`
    since its purchase_date. Always False if staleness reminders are
    disabled for this item (`stale_after_days` of 0 or -1) or the
    batch has already been fully consumed."""
    if item.stale_after_days <= 0:
        return False
    if batch.quantity <= 0:
        return False
    return (today - batch.purchase_date).days >= item.stale_after_days


def maybe_add_to_shopping_list(
    household_id: str,
    item_id: str,
    batches: list[Batch],
    existing_entries: list[ShoppingListEntry],
) -> ShoppingListEntry | None:
    """Return a new auto `ShoppingListEntry` if `item_id`'s total quantity
    across `batches` has hit 0 and no entry (auto or manual) for it
    already exists in `existing_entries`; otherwise return None. Does
    not mutate `existing_entries` — the caller persists the result."""
    total = sum(b.quantity for b in batches if b.item_id == item_id)
    if total > 0:
        return None
    if any(e.item_id == item_id for e in existing_entries):
        return None
    return ShoppingListEntry(
        household_id=household_id, item_id=item_id, source="auto"
    )
