"""Tests for core business logic (grocery_agent.services)."""

from datetime import date, timedelta

import pytest

from grocery_agent.dataclass import Item, ShoppingListEntry
from grocery_agent.services import (
    consume_from_items,
    is_expiring_soon,
    is_stale,
    maybe_add_to_shopping_list,
)

TODAY = date(2026, 1, 15)


def make_item(quantity, purchase_date=TODAY, expiry_date=None, stale_after_days=5):
    return Item(
        household_id="h1",
        product_id="apple",
        quantity=quantity,
        purchase_date=purchase_date,
        expiry_date=expiry_date,
        stale_after_days=stale_after_days,
    )


# --- consume_from_items -----------------------------------------------


def test_consume_deducts_from_soonest_expiring_item_first():
    soon = make_item(3, expiry_date=date(2026, 1, 16))
    later = make_item(3, expiry_date=date(2026, 1, 20))

    consume_from_items([soon, later], 2)

    assert soon.quantity == 1
    assert later.quantity == 3


def test_consume_spills_into_next_item_when_first_is_exhausted():
    soon = make_item(2, expiry_date=date(2026, 1, 16))
    later = make_item(3, expiry_date=date(2026, 1, 20))

    consume_from_items([soon, later], 4)

    assert soon.quantity == 0
    assert later.quantity == 1


def test_consume_treats_no_expiry_items_as_last():
    no_expiry = make_item(5, expiry_date=None)
    has_expiry = make_item(2, expiry_date=date(2026, 1, 16))

    consume_from_items([no_expiry, has_expiry], 3)

    assert has_expiry.quantity == 0
    assert no_expiry.quantity == 4


def test_consume_floors_at_zero_when_over_requested():
    item = make_item(2, expiry_date=date(2026, 1, 16))

    consume_from_items([item], 5)  # over-reporting isn't an error

    assert item.quantity == 0


def test_consume_floors_at_zero_across_multiple_items():
    first = make_item(2, expiry_date=date(2026, 1, 16))
    second = make_item(1, expiry_date=date(2026, 1, 20))

    consume_from_items([first, second], 10)

    assert first.quantity == 0
    assert second.quantity == 0


def test_consume_rejects_negative_quantity():
    item = make_item(2)
    with pytest.raises(ValueError):
        consume_from_items([item], -1)


# --- is_expiring_soon ----------------------------------------------------


def test_is_expiring_soon_true_within_threshold():
    item = make_item(1, expiry_date=TODAY + timedelta(days=2))
    assert is_expiring_soon(item, TODAY) is True


def test_is_expiring_soon_false_outside_threshold():
    item = make_item(1, expiry_date=TODAY + timedelta(days=3))
    assert is_expiring_soon(item, TODAY) is False


def test_is_expiring_soon_true_when_already_past_expiry():
    item = make_item(1, expiry_date=TODAY - timedelta(days=1))
    assert is_expiring_soon(item, TODAY) is True


def test_is_expiring_soon_false_with_no_expiry_date():
    item = make_item(1, expiry_date=None)
    assert is_expiring_soon(item, TODAY) is False


# --- is_stale --------------------------------------------------------------


def test_is_stale_true_after_threshold_with_stock_remaining():
    item = make_item(1, purchase_date=TODAY - timedelta(days=5), stale_after_days=5)
    assert is_stale(item, TODAY) is True


def test_is_stale_false_before_threshold():
    item = make_item(1, purchase_date=TODAY - timedelta(days=4), stale_after_days=5)
    assert is_stale(item, TODAY) is False


def test_is_stale_false_when_disabled_for_item():
    item = make_item(1, purchase_date=TODAY - timedelta(days=999), stale_after_days=0)
    assert is_stale(item, TODAY) is False


def test_is_stale_false_when_fully_consumed():
    item = make_item(0, purchase_date=TODAY - timedelta(days=10), stale_after_days=5)
    assert is_stale(item, TODAY) is False


# --- maybe_add_to_shopping_list --------------------------------------------


def test_adds_auto_entry_when_stock_hits_zero():
    item = make_item(0)
    entry = maybe_add_to_shopping_list("h1", "apple", [item], [])
    assert entry is not None
    assert entry.source == "auto"
    assert entry.product_id == "apple"


def test_does_not_add_when_stock_remains():
    item = make_item(1)
    entry = maybe_add_to_shopping_list("h1", "apple", [item], [])
    assert entry is None


def test_does_not_add_when_manual_entry_already_exists():
    item = make_item(0)
    existing = ShoppingListEntry(household_id="h1", product_id="apple", source="manual")
    entry = maybe_add_to_shopping_list("h1", "apple", [item], [existing])
    assert entry is None


def test_does_not_add_duplicate_auto_entry():
    item = make_item(0)
    existing = ShoppingListEntry(household_id="h1", product_id="apple", source="auto")
    entry = maybe_add_to_shopping_list("h1", "apple", [item], [existing])
    assert entry is None
