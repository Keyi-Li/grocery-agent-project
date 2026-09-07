"""One-time setup script: register a real household + user.

The app's `users.id` must match the Supabase Auth user's id exactly
(that's what verify_supabase_token in grocery_agent.api looks up), so
this script finds the Supabase Auth user by email via the Admin API
(using SUPABASE_SERVICE_ROLE_KEY) rather than generating a fresh id.

Prerequisite: create the Supabase Auth user first (Supabase Dashboard
-> Authentication -> Users -> Add user), then run:

    python scripts/setup_household.py --email you@example.com \\
        --household-name "The Lis" [--telegram-chat-id -100123456789]

Run scripts/get_telegram_chat_id.py first if you don't have the chat
id yet.
"""

from __future__ import annotations

import argparse
import os

import httpx
from dotenv import load_dotenv

from grocery_agent.dataclass import Household, User
from grocery_agent.db import get_connection
from grocery_agent.repositories import HouseholdRepository, UserRepository

load_dotenv()


def find_supabase_auth_user(email: str) -> dict | None:
    response = httpx.get(
        f"{os.environ['SUPABASE_URL']}/auth/v1/admin/users",
        headers={
            "apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}",
        },
        timeout=10,
    )
    response.raise_for_status()
    for candidate in response.json().get("users", []):
        if candidate.get("email") == email:
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--household-name", required=True)
    parser.add_argument("--telegram-chat-id", default=None)
    args = parser.parse_args()

    auth_user = find_supabase_auth_user(args.email)
    if auth_user is None:
        raise SystemExit(
            f"No Supabase Auth user with email {args.email!r}. Create one first "
            "via Supabase Dashboard -> Authentication -> Users -> Add user."
        )

    conn = get_connection()
    try:
        user_repo = UserRepository(conn)
        household_repo = HouseholdRepository(conn)

        user = user_repo.get(auth_user["id"])
        if user is None:
            user = User(id=auth_user["id"], email=args.email)
            user_repo.create(user)
            print(f"Created app user row for {args.email} ({user.id})")
        else:
            print(f"User row already exists for {args.email} ({user.id})")

        household = Household(
            name=args.household_name,
            member_ids=[user.id],
            telegram_chat_id=args.telegram_chat_id,
        )
        household_repo.create(household)
        conn.commit()
        print(f"Created household {household.name!r} (id={household.id})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
