"""Reminder checking and Telegram delivery.

Checked opportunistically on every backend request for whichever
household that request touched — no cron needed. Uses the
is_expiring_soon/is_stale predicates and ReminderState to decide
what's actually due, then delivers via Telegram.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg

from grocery_agent.llm import generate_reply
from grocery_agent.notifications import send_telegram_message
from grocery_agent.repositories import HouseholdRepository, InventoryRepository, ReminderStateRepository
from grocery_agent.services import is_expiring_soon, is_stale

# Staleness reminders repeat every 2 days until the item is consumed.
STALENESS_REPEAT_DAYS = 2


def run_reminder_check(conn: psycopg.Connection, household_id: str) -> list[str]:
    """Sends any newly-due reminders for `household_id`'s items.
    Returns the messages sent (empty if the household has no Telegram
    group configured yet, or nothing is due)."""
    household = HouseholdRepository(conn).get(household_id)
    if household is None or not household.telegram_chat_id:
        return []

    inventory_repo = InventoryRepository(conn)
    reminder_repo = ReminderStateRepository(conn)
    today = date.today()
    now = datetime.now(timezone.utc)
    sent: list[str] = []

    for product in inventory_repo.get_all_products_for_household(household_id):
        for item in inventory_repo.get_items(household_id, product.id):
            if item.quantity <= 0:
                continue

            if is_expiring_soon(item, today):
                if reminder_repo.get(item.id, "expiry") is None:  # one-time only
                    message = generate_reply(
                        None,
                        [
                            {
                                "action": "expiry_reminder",
                                "name": product.name,
                                "expiry_date": str(item.expiry_date),
                            }
                        ],
                    )
                    send_telegram_message(household.telegram_chat_id, message)
                    reminder_repo.upsert(item.id, "expiry", last_sent_at=now)
                    sent.append(message)

            if is_stale(item, today):
                state = reminder_repo.get(item.id, "staleness")
                due = state is None or (
                    state["active"]
                    and (now - state["last_sent_at"]).days >= STALENESS_REPEAT_DAYS
                )
                if due:
                    message = generate_reply(
                        None, [{"action": "staleness_reminder", "name": product.name}]
                    )
                    send_telegram_message(household.telegram_chat_id, message)
                    reminder_repo.upsert(item.id, "staleness", last_sent_at=now)
                    sent.append(message)

    return sent
