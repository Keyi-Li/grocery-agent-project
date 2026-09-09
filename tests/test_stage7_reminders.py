"""Tests for notifications + reminder checking.

send_telegram_message's actual HTTP call is monkeypatched (we don't
have a real Telegram group chat id to deliver to in CI) — everything
else (DB, ReminderState tracking, is_expiring_soon/is_stale) is real.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import grocery_agent.notifications as notifications_module
import grocery_agent.reminders as reminders_module
from grocery_agent.dataclass import Household, Item
from grocery_agent.reminders import run_reminder_check
from grocery_agent.repositories import (
    HouseholdRepository,
    InventoryRepository,
    ReminderStateRepository,
)

USER_ID = "test-user"


@pytest.fixture(autouse=True)
def mock_generate_reply(monkeypatch):
    # generate_reply is itself LLM-backed (live-tested in
    # test_stage5_llm.py) — deterministic here so reminder-logic
    # assertions (which items fired, how many times) aren't at the
    # mercy of the model's exact wording. Mirrors the real
    # reminder_digest fact shape (item names live inside
    # expiring_items/stale_items, not at the top level).
    def fake_generate_reply(context, facts):
        fact = facts[0]
        names = [i["name"] for i in fact.get("expiring_items", [])] + [
            i["name"] for i in fact.get("stale_items", [])
        ]
        return f"{', '.join(names)}: {fact['action']}"

    monkeypatch.setattr(reminders_module, "generate_reply", fake_generate_reply)


@pytest.fixture
def household_with_telegram(db_conn):
    household = Household(name="Reminders Test Household", telegram_chat_id="-100999")
    HouseholdRepository(db_conn).create(household)
    return household


@pytest.fixture
def sent_messages(monkeypatch):
    sent = []
    monkeypatch.setattr(
        reminders_module,
        "send_telegram_message",
        lambda chat_id, text: sent.append((chat_id, text)),
    )
    return sent


def test_send_telegram_message_posts_to_bot_api(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json

        return FakeResponse()

    monkeypatch.setattr(notifications_module.httpx, "post", fake_post)

    notifications_module.send_telegram_message("-100999", "hello")

    assert "sendMessage" in captured["url"]
    assert captured["json"] == {"chat_id": "-100999", "text": "hello"}


def test_no_reminders_when_household_has_no_telegram_chat_id(db_conn, sent_messages):
    household = Household(name="No Telegram Household")
    HouseholdRepository(db_conn).create(household)

    inventory_repo = InventoryRepository(db_conn)
    product = inventory_repo.get_or_create_product("yogurt")
    item = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=1,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=1),
    )
    inventory_repo.add_item(item)

    result = run_reminder_check(db_conn, household.id)

    assert result == []
    assert sent_messages == []


def test_expiry_reminder_fires_once(db_conn, household_with_telegram, sent_messages):
    household = household_with_telegram
    inventory_repo = InventoryRepository(db_conn)
    product = inventory_repo.get_or_create_product("yogurt")
    item = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=1,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=1),
    )
    inventory_repo.add_item(item)

    first = run_reminder_check(db_conn, household.id)
    second = run_reminder_check(db_conn, household.id)

    assert len(first) == 1
    assert "yogurt" in first[0]
    assert second == []  # one-time only, doesn't re-fire
    assert len(sent_messages) == 1


def test_staleness_reminder_repeats_after_interval(
    db_conn, household_with_telegram, sent_messages
):
    household = household_with_telegram
    inventory_repo = InventoryRepository(db_conn)
    product = inventory_repo.get_or_create_product("canned beans")
    item = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=1,
        purchase_date=date.today() - timedelta(days=5),
        stale_after_days=5,
    )
    inventory_repo.add_item(item)

    first = run_reminder_check(db_conn, household.id)
    assert len(first) == 1

    immediate_recheck = run_reminder_check(db_conn, household.id)
    assert immediate_recheck == []  # too soon to repeat

    # Simulate the repeat interval having elapsed.
    reminder_repo = ReminderStateRepository(db_conn)
    reminder_repo.update(
        item.id,
        "staleness",
        last_sent_at=datetime.now(timezone.utc) - timedelta(days=3),
    )

    later = run_reminder_check(db_conn, household.id)
    assert len(later) == 1
    assert len(sent_messages) == 2


def test_consuming_item_to_zero_deactivates_its_reminder(
    db_conn, household_with_telegram, sent_messages
):
    from grocery_agent.tools import consume_item

    household = household_with_telegram
    inventory_repo = InventoryRepository(db_conn)
    product = inventory_repo.get_or_create_product("canned beans")
    item = Item(
        household_id=household.id,
        product_id=product.id,
        quantity=1,
        purchase_date=date.today() - timedelta(days=5),
        stale_after_days=5,
    )
    inventory_repo.add_item(item)

    run_reminder_check(db_conn, household.id)  # fires staleness once
    consume_item(db_conn, household.id, USER_ID, "canned beans", 1)

    # The item row is deleted outright once fully consumed (not just
    # zeroed), so its reminder_state row cascades away with it — there's
    # nothing left to "deactivate."
    state = ReminderStateRepository(db_conn).get(item.id, "staleness")
    assert state is None
