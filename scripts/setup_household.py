"""One-time setup script: register a new household.

No user/auth registration needed — identity comes from Telegram
directly, and a household's membership is just whoever's in its
Telegram group. Run:

    python scripts/setup_household.py --household-name "The Lis" \\
        [--telegram-chat-id -100123456789]

Run scripts/get_telegram_chat_id.py first if you don't have the chat
id yet (or omit it and set it later via HouseholdRepository).
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from grocery_agent.dataclass import Household
from grocery_agent.db import get_connection
from grocery_agent.repositories import HouseholdRepository

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--household-name", required=True)
    parser.add_argument("--telegram-chat-id", default=None)
    args = parser.parse_args()

    conn = get_connection()
    try:
        household = Household(
            name=args.household_name, telegram_chat_id=args.telegram_chat_id
        )
        HouseholdRepository(conn).create(household)
        conn.commit()
        print(f"Created household {household.name!r} (id={household.id})")
        print("Set this as API_DEFAULT_HOUSEHOLD_ID in .env for /utterance testing.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
