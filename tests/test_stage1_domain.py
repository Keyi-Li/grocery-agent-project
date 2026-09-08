"""Tests for domain models (grocery_agent.dataclass)."""

from datetime import date

import pytest

from grocery_agent.dataclass import (
    ALLOWED_SHOPPING_LIST_SOURCES,
    Household,
    Item,
    Product,
    ShoppingListEntry,
)


# --- Household ----------------------------------------------------------


def test_household_construction():
    h = Household(name="The Lis")
    assert h.name == "The Lis"
    assert h.telegram_chat_id is None


def test_household_rejects_empty_name():
    with pytest.raises(ValueError):
        Household(name="   ")


# --- Product --------------------------------------------------------------


def test_product_construction_generates_id():
    p = Product(name="milk")
    assert p.name == "milk"
    assert p.id  # non-empty, auto-generated


def test_product_rejects_empty_name():
    with pytest.raises(ValueError):
        Product(name="")


# --- Item -----------------------------------------------------------------


def test_item_defaults_stale_after_days_to_five():
    i = Item(
        household_id="h1",
        product_id="p1",
        quantity=3,
        purchase_date=date(2026, 1, 1),
    )
    assert i.stale_after_days == 5


def test_item_supports_fractional_quantity():
    i = Item(
        household_id="h1",
        product_id="p1",
        quantity=0.5,
        purchase_date=date(2026, 1, 1),
    )
    assert i.quantity == 0.5


def test_item_rejects_negative_quantity():
    with pytest.raises(ValueError):
        Item(household_id="h1", product_id="p1", quantity=-1, purchase_date=date(2026, 1, 1))


@pytest.mark.parametrize("value", [0, 1, 30])
def test_item_accepts_valid_stale_after_days(value):
    i = Item(
        household_id="h1",
        product_id="p1",
        quantity=1,
        purchase_date=date(2026, 1, 1),
        stale_after_days=value,
    )
    assert i.stale_after_days == value


def test_item_rejects_negative_stale_after_days():
    with pytest.raises(ValueError):
        Item(
            household_id="h1",
            product_id="p1",
            quantity=1,
            purchase_date=date(2026, 1, 1),
            stale_after_days=-1,
        )


def test_item_expiry_date_defaults_to_none():
    i = Item(household_id="h1", product_id="p1", quantity=1, purchase_date=date(2026, 1, 1))
    assert i.expiry_date is None


# --- ShoppingListEntry --------------------------------------------------


@pytest.mark.parametrize("source", sorted(ALLOWED_SHOPPING_LIST_SOURCES))
def test_shopping_list_entry_accepts_valid_sources(source):
    entry = ShoppingListEntry(household_id="h1", product_id="p1", source=source)
    assert entry.source == source


def test_shopping_list_entry_rejects_invalid_source():
    with pytest.raises(ValueError):
        ShoppingListEntry(household_id="h1", product_id="p1", source="wishlist")
