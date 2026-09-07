"""Tests for Stage 2 core business logic (grocery_agent.services)."""

from datetime import date, timedelta

import pytest

from grocery_agent.dataclass import Batch, Item, ShoppingListEntry
from grocery_agent.services import (
    consume_from_batches,
    is_expiring_soon,
    is_stale,
    maybe_add_to_shopping_list,
)

TODAY = date(2026, 1, 15)


def make_batch(quantity, purchase_date=TODAY, expiry_date=None):
    return Batch(
        household_id="h1",
        item_id="apple",
        quantity=quantity,
        purchase_date=purchase_date,
        expiry_date=expiry_date,
    )


# --- consume_from_batches -----------------------------------------------


def test_consume_deducts_from_soonest_expiring_batch_first():
    soon = make_batch(3, expiry_date=date(2026, 1, 16))
    later = make_batch(3, expiry_date=date(2026, 1, 20))

    consume_from_batches([soon, later], 2)

    assert soon.quantity == 1
    assert later.quantity == 3


def test_consume_spills_into_next_batch_when_first_is_exhausted():
    soon = make_batch(2, expiry_date=date(2026, 1, 16))
    later = make_batch(3, expiry_date=date(2026, 1, 20))

    consume_from_batches([soon, later], 4)

    assert soon.quantity == 0
    assert later.quantity == 1


def test_consume_treats_no_expiry_batches_as_last():
    no_expiry = make_batch(5, expiry_date=None)
    has_expiry = make_batch(2, expiry_date=date(2026, 1, 16))

    consume_from_batches([no_expiry, has_expiry], 3)

    assert has_expiry.quantity == 0
    assert no_expiry.quantity == 4


def test_consume_raises_and_does_not_mutate_when_insufficient_stock():
    batch = make_batch(2, expiry_date=date(2026, 1, 16))

    with pytest.raises(ValueError):
        consume_from_batches([batch], 5)

    assert batch.quantity == 2


def test_consume_rejects_negative_quantity():
    batch = make_batch(2)
    with pytest.raises(ValueError):
        consume_from_batches([batch], -1)


# --- is_expiring_soon ----------------------------------------------------


def test_is_expiring_soon_true_within_threshold():
    batch = make_batch(1, expiry_date=TODAY + timedelta(days=2))
    assert is_expiring_soon(batch, TODAY) is True


def test_is_expiring_soon_false_outside_threshold():
    batch = make_batch(1, expiry_date=TODAY + timedelta(days=3))
    assert is_expiring_soon(batch, TODAY) is False


def test_is_expiring_soon_true_when_already_past_expiry():
    batch = make_batch(1, expiry_date=TODAY - timedelta(days=1))
    assert is_expiring_soon(batch, TODAY) is True


def test_is_expiring_soon_false_with_no_expiry_date():
    batch = make_batch(1, expiry_date=None)
    assert is_expiring_soon(batch, TODAY) is False


# --- is_stale --------------------------------------------------------------


def test_is_stale_true_after_threshold_with_stock_remaining():
    item = Item(name="apple", stale_after_days=5)
    batch = make_batch(1, purchase_date=TODAY - timedelta(days=5))
    assert is_stale(batch, item, TODAY) is True


def test_is_stale_false_before_threshold():
    item = Item(name="apple", stale_after_days=5)
    batch = make_batch(1, purchase_date=TODAY - timedelta(days=4))
    assert is_stale(batch, item, TODAY) is False


@pytest.mark.parametrize("disabled_value", [0, -1])
def test_is_stale_false_when_disabled_for_item(disabled_value):
    item = Item(name="canned beans", stale_after_days=disabled_value)
    batch = make_batch(1, purchase_date=TODAY - timedelta(days=999))
    assert is_stale(batch, item, TODAY) is False


def test_is_stale_false_when_batch_fully_consumed():
    item = Item(name="apple", stale_after_days=5)
    batch = make_batch(0, purchase_date=TODAY - timedelta(days=10))
    assert is_stale(batch, item, TODAY) is False


# --- maybe_add_to_shopping_list --------------------------------------------


def test_adds_auto_entry_when_stock_hits_zero():
    batch = make_batch(0)
    entry = maybe_add_to_shopping_list("h1", "apple", [batch], [])
    assert entry is not None
    assert entry.source == "auto"
    assert entry.item_id == "apple"


def test_does_not_add_when_stock_remains():
    batch = make_batch(1)
    entry = maybe_add_to_shopping_list("h1", "apple", [batch], [])
    assert entry is None


def test_does_not_add_when_manual_entry_already_exists():
    batch = make_batch(0)
    existing = ShoppingListEntry(household_id="h1", item_id="apple", source="manual")
    entry = maybe_add_to_shopping_list("h1", "apple", [batch], [existing])
    assert entry is None


def test_does_not_add_duplicate_auto_entry():
    batch = make_batch(0)
    existing = ShoppingListEntry(household_id="h1", item_id="apple", source="auto")
    entry = maybe_add_to_shopping_list("h1", "apple", [batch], [existing])
    assert entry is None
