"""Tests for the persistence layer (grocery_agent.repositories).

Runs against the real Supabase project via db_conn (see conftest.py);
each test's writes are rolled back afterward.
"""

from datetime import date, timedelta

from grocery_agent.dataclass import Household, Item, ShoppingListEntry
from grocery_agent.repositories import (
    HouseholdRepository,
    InventoryRepository,
    ShoppingListRepository,
)


def make_household(conn):
    household = Household(name="Test Household")
    HouseholdRepository(conn).create(household)
    return household


def test_household_round_trip(db_conn):
    household = make_household(db_conn)

    fetched = HouseholdRepository(db_conn).get(household.id)

    assert fetched is not None
    assert fetched.name == "Test Household"


def test_household_get_by_telegram_chat_id(db_conn):
    household = Household(name="Telegram Test Household", telegram_chat_id="-100987654321")
    HouseholdRepository(db_conn).create(household)

    fetched = HouseholdRepository(db_conn).get_by_telegram_chat_id("-100987654321")

    assert fetched is not None
    assert fetched.id == household.id


def test_get_or_create_product_is_idempotent(db_conn):
    repo = InventoryRepository(db_conn)

    first = repo.get_or_create_product("apple")
    second = repo.get_or_create_product("apple")

    assert first.id == second.id


def test_item_round_trip_and_fefo_ordering(db_conn):
    household = make_household(db_conn)
    repo = InventoryRepository(db_conn)
    product = repo.get_or_create_product("milk")

    later = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=2,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=10),
    )
    sooner = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=1,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=1),
    )
    no_expiry = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=3,
        purchase_date=date.today(),
    )
    repo.add_item(later)
    repo.add_item(sooner)
    repo.add_item(no_expiry)

    fetched = repo.get_items(household.id, product.id)

    assert [i.id for i in fetched] == [sooner.id, later.id, no_expiry.id]


def test_item_carries_stale_after_days(db_conn):
    household = make_household(db_conn)
    repo = InventoryRepository(db_conn)
    product = repo.get_or_create_product("fish")

    item = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=1,
        purchase_date=date.today(),
        stale_after_days=2,
    )
    repo.add_item(item)

    fetched = repo.get_items(household.id, product.id)[0]
    assert fetched.stale_after_days == 2


def test_save_item_quantity_persists_mutation(db_conn):
    household = make_household(db_conn)
    repo = InventoryRepository(db_conn)
    product = repo.get_or_create_product("eggs")

    item = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=12,
        purchase_date=date.today(),
    )
    repo.add_item(item)

    item.quantity = 6
    repo.save_item_quantity(item)

    fetched = repo.get_items(household.id, product.id)[0]
    assert fetched.quantity == 6


def test_shopping_list_round_trip(db_conn):
    household = make_household(db_conn)
    inventory_repo = InventoryRepository(db_conn)
    list_repo = ShoppingListRepository(db_conn)
    product = inventory_repo.get_or_create_product("bread")

    entry = ShoppingListEntry(
        household_id=household.id, product_id=product.id, source="auto"
    )
    list_repo.add_entry(entry)

    entries = list_repo.get_entries(household.id)
    assert len(entries) == 1
    assert entries[0].product_id == product.id
    assert entries[0].source == "auto"

    list_repo.remove_entry(entry.id)
    assert list_repo.get_entries(household.id) == []
