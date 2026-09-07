"""Tests for Stage 3 persistence layer (grocery_agent.repositories).

Runs against the real Supabase project via db_conn (see conftest.py);
each test's writes are rolled back afterward.
"""

from datetime import date, timedelta

from grocery_agent.dataclass import Household, ShoppingListEntry, User
from grocery_agent.repositories import (
    HouseholdRepository,
    InventoryRepository,
    ShoppingListRepository,
    UserRepository,
)


def make_household(conn, member_ids=None):
    user_repo = UserRepository(conn)
    household_repo = HouseholdRepository(conn)
    user = User(email=f"{date.today()}-test@example.com")
    user_repo.create(user)
    household = Household(name="Test Household", member_ids=member_ids or [user.id])
    household_repo.create(household)
    return household, user


def test_household_round_trip(db_conn):
    household, _user = make_household(db_conn)

    repo = HouseholdRepository(db_conn)
    fetched = repo.get(household.id)

    assert fetched is not None
    assert fetched.name == "Test Household"
    assert fetched.member_ids == household.member_ids


def test_household_set_telegram_chat_id(db_conn):
    household, _user = make_household(db_conn)
    repo = HouseholdRepository(db_conn)

    repo.set_telegram_chat_id(household.id, "-100123456789")

    assert repo.get(household.id).telegram_chat_id == "-100123456789"


def test_household_add_member(db_conn):
    household, _user = make_household(db_conn)
    user_repo = UserRepository(db_conn)
    household_repo = HouseholdRepository(db_conn)

    new_user = User(email="roommate@example.com")
    user_repo.create(new_user)
    household_repo.add_member(household.id, new_user.id)

    fetched = household_repo.get(household.id)
    assert new_user.id in fetched.member_ids


def test_get_or_create_item_is_idempotent(db_conn):
    repo = InventoryRepository(db_conn)

    first = repo.get_or_create_item("apple", stale_after_days=5)
    second = repo.get_or_create_item("apple", stale_after_days=999)

    assert first.id == second.id
    assert second.stale_after_days == 5  # unchanged by the second call


def test_batch_round_trip_and_fefo_ordering(db_conn):
    household, _user = make_household(db_conn)
    repo = InventoryRepository(db_conn)
    item = repo.get_or_create_item("milk")

    from grocery_agent.dataclass import Batch

    later = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=2,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=10),
    )
    sooner = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=1,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=1),
    )
    no_expiry = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=3,
        purchase_date=date.today(),
    )
    repo.add_batch(later)
    repo.add_batch(sooner)
    repo.add_batch(no_expiry)

    fetched = repo.get_batches(household.id, item.id)

    assert [b.id for b in fetched] == [sooner.id, later.id, no_expiry.id]


def test_save_batch_quantity_persists_mutation(db_conn):
    household, _user = make_household(db_conn)
    repo = InventoryRepository(db_conn)
    item = repo.get_or_create_item("eggs")

    from grocery_agent.dataclass import Batch

    batch = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=12,
        purchase_date=date.today(),
    )
    repo.add_batch(batch)

    batch.quantity = 6
    repo.save_batch_quantity(batch)

    fetched = repo.get_batches(household.id, item.id)[0]
    assert fetched.quantity == 6


def test_shopping_list_round_trip(db_conn):
    household, _user = make_household(db_conn)
    inventory_repo = InventoryRepository(db_conn)
    list_repo = ShoppingListRepository(db_conn)
    item = inventory_repo.get_or_create_item("bread")

    entry = ShoppingListEntry(
        household_id=household.id, item_id=item.id, source="auto"
    )
    list_repo.add_entry(entry)

    entries = list_repo.get_entries(household.id)
    assert len(entries) == 1
    assert entries[0].item_id == item.id
    assert entries[0].source == "auto"

    list_repo.remove_entry(entry.id)
    assert list_repo.get_entries(household.id) == []
