"""Tests for Stage 1 domain models (grocery_agent.models)."""

from datetime import date

import pytest

from grocery_agent.dataclass import (
    ALLOWED_UNITS,
    Batch,
    Household,
    Item,
    ShoppingListEntry,
    User,
)


# --- User -------------------------------------------------------------


def test_user_construction_generates_id():
    u = User(email="a@example.com")
    assert u.email == "a@example.com"
    assert u.id  # non-empty, auto-generated


def test_user_rejects_invalid_email():
    with pytest.raises(ValueError):
        User(email="not-an-email")

    with pytest.raises(ValueError):
        User(email="")


# --- Household ----------------------------------------------------------


def test_household_construction():
    h = Household(name="The Lis")
    assert h.name == "The Lis"
    assert h.member_ids == []


def test_household_rejects_empty_name():
    with pytest.raises(ValueError):
        Household(name="   ")


# --- Item -----------------------------------------------------------------


def test_item_default_stale_after_days():
    item = Item(name="milk")
    assert item.stale_after_days == 5


@pytest.mark.parametrize("value", [-1, 0, 1, 30])
def test_item_accepts_valid_stale_after_days(value):
    item = Item(name="canned beans", stale_after_days=value)
    assert item.stale_after_days == value


def test_item_rejects_stale_after_days_below_negative_one():
    with pytest.raises(ValueError):
        Item(name="milk", stale_after_days=-2)


def test_item_rejects_empty_name():
    with pytest.raises(ValueError):
        Item(name="")


# --- Batch ------------------------------------------------------------------


def test_batch_defaults_unit_to_unit():
    b = Batch(
        household_id="h1",
        item_id="i1",
        quantity=3,
        purchase_date=date(2026, 1, 1),
    )
    assert b.unit == "unit"


def test_batch_supports_fractional_quantity():
    b = Batch(
        household_id="h1",
        item_id="i1",
        quantity=0.5,
        purchase_date=date(2026, 1, 1),
        unit="lb",
    )
    assert b.quantity == 0.5


def test_batch_rejects_negative_quantity():
    with pytest.raises(ValueError):
        Batch(
            household_id="h1",
            item_id="i1",
            quantity=-1,
            purchase_date=date(2026, 1, 1),
        )


def test_batch_rejects_unit_outside_allowed_set():
    with pytest.raises(ValueError):
        Batch(
            household_id="h1",
            item_id="i1",
            quantity=1,
            purchase_date=date(2026, 1, 1),
            unit="gallon",
        )


def test_batch_allows_every_unit_in_allowed_set():
    for unit in ALLOWED_UNITS:
        b = Batch(
            household_id="h1",
            item_id="i1",
            quantity=1,
            purchase_date=date(2026, 1, 1),
            unit=unit,
        )
        assert b.unit == unit


def test_batch_expiry_date_defaults_to_none():
    b = Batch(
        household_id="h1",
        item_id="i1",
        quantity=1,
        purchase_date=date(2026, 1, 1),
    )
    assert b.expiry_date is None


# --- ShoppingListEntry --------------------------------------------------


@pytest.mark.parametrize("source", ["auto", "manual"])
def test_shopping_list_entry_accepts_valid_sources(source):
    entry = ShoppingListEntry(household_id="h1", item_id="i1", source=source)
    assert entry.source == source
    assert entry.created_at is not None


def test_shopping_list_entry_rejects_invalid_source():
    with pytest.raises(ValueError):
        ShoppingListEntry(household_id="h1", item_id="i1", source="wishlist")
