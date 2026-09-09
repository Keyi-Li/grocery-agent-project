"""Reminder checking and Telegram delivery.

Deliberately decoupled from user requests (see api.check_reminders) —
a household with no activity should still get notified about
expiring/stale items, so this runs on an external schedule instead.
Uses the is_expiring_soon/is_stale predicates and ReminderState to
decide what's actually due, then delivers via Telegram.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from grocery_agent.llm import generate_reply
from grocery_agent.notifications import send_telegram_message
from grocery_agent.repositories import HouseholdRepository, InventoryRepository, ReminderStateRepository
from grocery_agent.services import is_expiring_soon, is_stale

# Staleness reminders repeat every 2 days until the item is consumed.
STALENESS_REPEAT_DAYS = 2

# What "daily" means: the scheduled trigger runs hourly (see
# api.check_reminders), and each household only gets checked when it's
# currently this hour in *their own* timezone (Household.timezone) —
# so one shared hourly trigger still lands once a day per household,
# regardless of timezone.
REMINDER_DIGEST_HOUR = 18  # 6pm


def run_reminder_check(conn: psycopg.Connection, household_id: str) -> list[str]:
    """Sends one grouped digest of newly-due reminders for
    `household_id` — a single message covering every expiring/stale
    item due right now, not one message per item. Returns the digest
    as a single-element list (empty if the household has no Telegram
    group configured, or nothing is due)."""
    household = HouseholdRepository(conn).get(household_id)
    if household is None or not household.telegram_chat_id:
        return []

    inventory_repo = InventoryRepository(conn)
    reminder_repo = ReminderStateRepository(conn)
    today = date.today()
    now = datetime.now(timezone.utc)

    expiring_items: list[dict] = []
    stale_items: list[dict] = []
    due_expiry_ids: list[str] = []
    due_staleness_ids: list[str] = []

    for product in inventory_repo.get_all_products_for_household(household_id):
        for item in inventory_repo.get_items(household_id, product.id):
            if item.quantity <= 0:
                continue

            if is_expiring_soon(item, today):
                if reminder_repo.get(item.id, "expiry") is None:  # one-time only
                    expiring_items.append(
                        {
                            "name": product.name,
                            "quantity": item.quantity,
                            "expiry_date": str(item.expiry_date),
                        }
                    )
                    due_expiry_ids.append(item.id)

            if is_stale(item, today):
                state = reminder_repo.get(item.id, "staleness")
                due = state is None or (
                    state["active"]
                    and (now - state["last_sent_at"]).days >= STALENESS_REPEAT_DAYS
                )
                if due:
                    stale_items.append(
                        {
                            "name": product.name,
                            "quantity": item.quantity,
                            "purchase_date": str(item.purchase_date),
                        }
                    )
                    due_staleness_ids.append(item.id)

    if not expiring_items and not stale_items:
        return []

    message = generate_reply(
        None,
        [
            {
                "action": "reminder_digest",
                "expiring_items": expiring_items,
                "stale_items": stale_items,
            }
        ],
        household.language,
    )
    send_telegram_message(household.telegram_chat_id, message)

    for item_id in due_expiry_ids:
        reminder_repo.update(item_id, "expiry", last_sent_at=now)
    for item_id in due_staleness_ids:
        reminder_repo.update(item_id, "staleness", last_sent_at=now)

    return [message]


def run_reminder_check_for_all_households(conn: psycopg.Connection) -> dict[str, list[str]]:
    """Runs run_reminder_check for every household whose local time is
    currently REMINDER_DIGEST_HOUR — the entry point for a scheduled
    trigger (see api.check_reminders) that runs hourly, so each
    household's own local digest time gets caught regardless of which
    timezone they're in. A household with an unrecognized timezone
    value is skipped rather than crashing the whole batch."""
    now_utc = datetime.now(timezone.utc)
    results: dict[str, list[str]] = {}
    for household in HouseholdRepository(conn).get_all():
        try:
            local_hour = now_utc.astimezone(ZoneInfo(household.timezone)).hour
        except ZoneInfoNotFoundError:
            continue
        if local_hour != REMINDER_DIGEST_HOUR:
            continue
        sent = run_reminder_check(conn, household.id)
        if sent:
            results[household.id] = sent
    return results
