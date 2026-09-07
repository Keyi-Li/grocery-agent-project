"""Stage 7 — reminder checking + Telegram delivery.

Checked opportunistically on every backend request for whichever
household that request touched (plan Section 3b: no cron needed for
v1). Uses Stage 2's is_expiring_soon/is_stale predicates and
ReminderState (Stage 3 schema) to decide what's actually due, then
Telegram (Section 3c) for delivery.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg

from grocery_agent.notifications import send_telegram_message
from grocery_agent.repositories import HouseholdRepository, InventoryRepository, ReminderStateRepository
from grocery_agent.services import is_expiring_soon, is_stale

# Plan Section 3b: staleness reminders repeat every 2 days until consumed.
STALENESS_REPEAT_DAYS = 2


def run_reminder_check(conn: psycopg.Connection, household_id: str) -> list[str]:
    """Sends any newly-due reminders for `household_id`'s batches.
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

    for item in inventory_repo.get_all_items_for_household(household_id):
        for batch in inventory_repo.get_batches(household_id, item.id):
            if batch.quantity <= 0:
                continue

            if is_expiring_soon(batch, today):
                if reminder_repo.get(batch.id, "expiry") is None:  # one-time only
                    message = f"{item.name} 快过期了（{batch.expiry_date}）。"
                    send_telegram_message(household.telegram_chat_id, message)
                    reminder_repo.upsert(batch.id, "expiry", last_sent_at=now)
                    sent.append(message)

            if is_stale(batch, item, today):
                state = reminder_repo.get(batch.id, "staleness")
                due = state is None or (
                    state["active"]
                    and (now - state["last_sent_at"]).days >= STALENESS_REPEAT_DAYS
                )
                if due:
                    message = f"{item.name} 放了很久没动，还在吗？"
                    send_telegram_message(household.telegram_chat_id, message)
                    reminder_repo.upsert(batch.id, "staleness", last_sent_at=now)
                    sent.append(message)

    return sent
