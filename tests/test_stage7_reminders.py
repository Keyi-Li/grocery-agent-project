"""Tests for Stage 7 notifications + reminder checking.

send_telegram_message's actual HTTP call is monkeypatched (we don't
have a real Telegram group chat id to deliver to in CI) — everything
else (DB, ReminderState tracking, is_expiring_soon/is_stale) is real.
The plan explicitly calls the final live-delivery check a manual test
(Stage 7), so that part is left for you to verify by hand once a
household has a real telegram_chat_id.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import grocery_agent.notifications as notifications_module
import grocery_agent.reminders as reminders_module
from grocery_agent.dataclass import Batch, Household, User
from grocery_agent.reminders import run_reminder_check
from grocery_agent.repositories import (
    HouseholdRepository,
    InventoryRepository,
    ReminderStateRepository,
    UserRepository,
)


@pytest.fixture
def household_with_telegram(db_conn):
    user = User(email="reminders-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(
        name="Reminders Test Household",
        member_ids=[user.id],
        telegram_chat_id="-100999",
    )
    HouseholdRepository(db_conn).create(household)
    return household, user


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
    user = User(email="no-telegram@example.com")
    UserRepository(db_conn).create(user)
    household = Household(name="No Telegram Household", member_ids=[user.id])
    HouseholdRepository(db_conn).create(household)

    inventory_repo = InventoryRepository(db_conn)
    item = inventory_repo.get_or_create_item("yogurt")
    batch = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=1,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=1),
    )
    inventory_repo.add_batch(batch)

    result = run_reminder_check(db_conn, household.id)

    assert result == []
    assert sent_messages == []


def test_expiry_reminder_fires_once(db_conn, household_with_telegram, sent_messages):
    household, _user = household_with_telegram
    inventory_repo = InventoryRepository(db_conn)
    item = inventory_repo.get_or_create_item("yogurt")
    batch = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=1,
        purchase_date=date.today(),
        expiry_date=date.today() + timedelta(days=1),
    )
    inventory_repo.add_batch(batch)

    first = run_reminder_check(db_conn, household.id)
    second = run_reminder_check(db_conn, household.id)

    assert len(first) == 1
    assert "yogurt" in first[0]
    assert second == []  # one-time only, doesn't re-fire
    assert len(sent_messages) == 1


def test_staleness_reminder_repeats_after_interval(
    db_conn, household_with_telegram, sent_messages
):
    household, _user = household_with_telegram
    inventory_repo = InventoryRepository(db_conn)
    item = inventory_repo.get_or_create_item(
        "canned beans"
    )  # default stale_after_days=5
    batch = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=1,
        purchase_date=date.today() - timedelta(days=5),
    )
    inventory_repo.add_batch(batch)

    first = run_reminder_check(db_conn, household.id)
    assert len(first) == 1

    immediate_recheck = run_reminder_check(db_conn, household.id)
    assert immediate_recheck == []  # too soon to repeat

    # Simulate the repeat interval having elapsed.
    reminder_repo = ReminderStateRepository(db_conn)
    state = reminder_repo.get(batch.id, "staleness")
    reminder_repo.upsert(
        batch.id,
        "staleness",
        last_sent_at=datetime.now(timezone.utc) - timedelta(days=3),
    )

    later = run_reminder_check(db_conn, household.id)
    assert len(later) == 1
    assert len(sent_messages) == 2


def test_consuming_batch_to_zero_deactivates_its_reminder(
    db_conn, household_with_telegram, sent_messages
):
    from grocery_agent.tools import consume_item

    household, user = household_with_telegram
    inventory_repo = InventoryRepository(db_conn)
    item = inventory_repo.get_or_create_item("canned beans")
    batch = Batch(
        household_id=household.id,
        item_id=item.id,
        quantity=1,
        purchase_date=date.today() - timedelta(days=5),
    )
    inventory_repo.add_batch(batch)

    run_reminder_check(db_conn, household.id)  # fires staleness once
    consume_item(db_conn, household.id, user.id, "canned beans", 1)

    state = ReminderStateRepository(db_conn).get(batch.id, "staleness")
    assert state["active"] is False
