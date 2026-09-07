"""Tests for Stage 4 tool functions (grocery_agent.tools)."""

from datetime import date, timedelta

import pytest

from grocery_agent.dataclass import Household, User
from grocery_agent.repositories import (
    ActionLogRepository,
    HouseholdRepository,
    InventoryRepository,
    UserRepository,
)
from grocery_agent.tools import (
    add_item,
    add_to_shopping_list,
    consume_item,
    query_expiring_soon,
    query_shopping_list,
    query_stock,
)


@pytest.fixture
def household_and_user(db_conn):
    user = User(email="tools-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(name="Tools Test Household", member_ids=[user.id])
    HouseholdRepository(db_conn).create(household)
    return household, user


def test_add_item_creates_batch_and_logs_action(db_conn, household_and_user):
    household, user = household_and_user

    batch = add_item(db_conn, household.id, user.id, "apple", 3, unit="unit")

    stock = query_stock(db_conn, household.id, "apple")
    assert stock == [{"name": "apple", "quantity": 3, "unit": "unit"}]

    logs = ActionLogRepository(db_conn).get_for_household(household.id)
    assert len(logs) == 1
    assert logs[0]["action"] == "add_item"
    assert logs[0]["details"]["name"] == "apple"
    assert logs[0]["details"]["quantity"] == 3


def test_consume_item_deducts_stock_and_logs_action(db_conn, household_and_user):
    household, user = household_and_user
    add_item(db_conn, household.id, user.id, "milk", 2, unit="unit")

    consume_item(db_conn, household.id, user.id, "milk", 1)

    stock = query_stock(db_conn, household.id, "milk")
    assert stock == [{"name": "milk", "quantity": 1, "unit": "unit"}]

    logs = ActionLogRepository(db_conn).get_for_household(household.id)
    assert logs[-1]["action"] == "consume_item"


def test_consume_item_to_zero_auto_adds_to_shopping_list(db_conn, household_and_user):
    household, user = household_and_user
    add_item(db_conn, household.id, user.id, "eggs", 1, unit="unit")

    consume_item(db_conn, household.id, user.id, "eggs", 1)

    entries = query_shopping_list(db_conn, household.id)
    assert len(entries) == 1
    assert entries[0].source == "auto"

    item = InventoryRepository(db_conn).get_or_create_item("eggs")
    assert entries[0].item_id == item.id


def test_add_to_shopping_list_is_manual_and_idempotent(db_conn, household_and_user):
    household, user = household_and_user

    entry1 = add_to_shopping_list(db_conn, household.id, user.id, "bread")
    entry2 = add_to_shopping_list(db_conn, household.id, user.id, "bread")

    assert entry1.source == "manual"
    assert entry1.id == entry2.id  # second call didn't create a duplicate
    assert len(query_shopping_list(db_conn, household.id)) == 1


def test_query_stock_without_name_lists_everything(db_conn, household_and_user):
    household, user = household_and_user
    add_item(db_conn, household.id, user.id, "apple", 3)
    add_item(db_conn, household.id, user.id, "milk", 1)

    stock = query_stock(db_conn, household.id)

    assert {(s["name"], s["quantity"]) for s in stock} == {
        ("apple", 3),
        ("milk", 1),
    }


def test_query_expiring_soon_only_returns_near_expiry_batches(
    db_conn, household_and_user
):
    household, user = household_and_user
    add_item(
        db_conn,
        household.id,
        user.id,
        "yogurt",
        1,
        expiry_date=date.today() + timedelta(days=1),
    )
    add_item(
        db_conn,
        household.id,
        user.id,
        "canned beans",
        1,
        expiry_date=date.today() + timedelta(days=30),
    )

    expiring = query_expiring_soon(db_conn, household.id)

    assert [e["name"] for e in expiring] == ["yogurt"]
