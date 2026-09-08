"""Tests for tool functions (grocery_agent.tools)."""

from datetime import date, timedelta

import pytest

from grocery_agent.dataclass import Household
from grocery_agent.repositories import ActionLogRepository, HouseholdRepository, InventoryRepository
from grocery_agent.tools import (
    add_item,
    add_to_shopping_list,
    consume_item,
    query_expiring_soon,
    query_item_details,
    query_shopping_list,
    query_stock,
)

USER_ID = "test-user"


@pytest.fixture
def household(db_conn):
    household = Household(name="Tools Test Household")
    HouseholdRepository(db_conn).create(household)
    return household


def test_add_item_creates_item_and_logs_action(db_conn, household):
    add_item(db_conn, household.id, USER_ID, "apple", 3)

    stock = query_stock(db_conn, household.id, "apple")
    assert stock == [{"name": "apple", "quantity": 3}]

    logs = ActionLogRepository(db_conn).get_for_household(household.id)
    assert len(logs) == 1
    assert logs[0]["action"] == "add_item"
    assert logs[0]["details"]["name"] == "apple"
    assert logs[0]["details"]["quantity"] == 3


def test_add_item_accepts_explicit_purchase_date(db_conn, household):
    backdated = date.today() - timedelta(days=3)
    add_item(db_conn, household.id, USER_ID, "rice", 1, purchase_date=backdated)

    details = query_item_details(db_conn, household.id, "rice")
    assert details[0]["purchase_date"] == backdated


def test_consume_item_deducts_stock_and_logs_action(db_conn, household):
    add_item(db_conn, household.id, USER_ID, "milk", 2)

    consume_item(db_conn, household.id, USER_ID, "milk", 1)

    stock = query_stock(db_conn, household.id, "milk")
    assert stock == [{"name": "milk", "quantity": 1}]

    logs = ActionLogRepository(db_conn).get_for_household(household.id)
    assert logs[-1]["action"] == "consume_item"


def test_consume_item_to_zero_auto_adds_to_shopping_list(db_conn, household):
    add_item(db_conn, household.id, USER_ID, "eggs", 1)

    consume_item(db_conn, household.id, USER_ID, "eggs", 1)

    entries = query_shopping_list(db_conn, household.id)
    assert len(entries) == 1
    assert entries[0].source == "auto"

    product = InventoryRepository(db_conn).get_or_create_product("eggs")
    assert entries[0].product_id == product.id


def test_query_stock_omits_fully_consumed_items(db_conn, household):
    add_item(db_conn, household.id, USER_ID, "eggs", 1)
    add_item(db_conn, household.id, USER_ID, "milk", 2)
    consume_item(db_conn, household.id, USER_ID, "eggs", 1)

    everything = query_stock(db_conn, household.id)
    assert everything == [{"name": "milk", "quantity": 2}]

    named = query_stock(db_conn, household.id, "eggs")
    assert named == []  # empty, not [{"name": "eggs", "quantity": 0}]


def test_add_to_shopping_list_is_manual_and_idempotent(db_conn, household):
    entry1 = add_to_shopping_list(db_conn, household.id, USER_ID, "bread")
    entry2 = add_to_shopping_list(db_conn, household.id, USER_ID, "bread")

    assert entry1.source == "manual"
    assert entry1.id == entry2.id  # second call didn't create a duplicate
    assert len(query_shopping_list(db_conn, household.id)) == 1


def test_query_stock_without_name_lists_everything(db_conn, household):
    add_item(db_conn, household.id, USER_ID, "apple", 3)
    add_item(db_conn, household.id, USER_ID, "milk", 1)

    stock = query_stock(db_conn, household.id)

    assert {(s["name"], s["quantity"]) for s in stock} == {
        ("apple", 3),
        ("milk", 1),
    }


def test_query_expiring_soon_only_returns_near_expiry_items(db_conn, household):
    add_item(
        db_conn, household.id, USER_ID, "yogurt", 1, expiry_date=date.today() + timedelta(days=1)
    )
    add_item(
        db_conn, household.id, USER_ID, "canned beans", 1,
        expiry_date=date.today() + timedelta(days=30),
    )

    expiring = query_expiring_soon(db_conn, household.id)

    assert [e["name"] for e in expiring] == ["yogurt"]


def test_query_item_details_returns_purchase_and_expiry_dates(db_conn, household):
    add_item(db_conn, household.id, USER_ID, "cheese", 1, expiry_date=date.today() + timedelta(days=10))

    details = query_item_details(db_conn, household.id, "cheese")

    assert len(details) == 1
    assert details[0]["quantity"] == 1
    assert details[0]["purchase_date"] == date.today()
    assert details[0]["expiry_date"] == date.today() + timedelta(days=10)


def test_query_item_details_empty_for_unknown_product(db_conn, household):
    assert query_item_details(db_conn, household.id, "nonexistent-item") == []
