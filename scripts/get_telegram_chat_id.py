"""One-time helper for Stage 7 setup (plan Section 3c).

Usage: create your household's Telegram group, add the bot to it,
send any message in the group, then run this script — it prints the
chat id(s) the bot has seen recent messages from, so you know what to
save as that household's telegram_chat_id.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    response = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    response.raise_for_status()
    updates = response.json()["result"]

    if not updates:
        print(
            "No updates yet. Add the bot to your group and send a message there, "
            "then run this again."
        )
        return

    seen = {}
    for update in updates:
        chat = update.get("message", {}).get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title") or chat.get("first_name") or "?"

    for chat_id, name in seen.items():
        print(f"chat_id={chat_id}  name={name!r}")


if __name__ == "__main__":
    main()
