"""Stage 7 — Telegram delivery (plan Section 3c).

One bot token (global secret in .env), one chat id per household
(Household.telegram_chat_id) — see docs/grocery-agent-plan.md
Section 3c for the setup steps.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def send_telegram_message(chat_id: str, text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    response.raise_for_status()


def download_telegram_file(file_id: str) -> bytes:
    """Fetches a file (e.g. a receipt photo) the bot received, via
    Telegram's two-step file API: resolve file_id -> file_path, then
    download from the file-serving host."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    info = httpx.get(
        f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=10
    )
    info.raise_for_status()
    file_path = info.json()["result"]["file_path"]
    content = httpx.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30)
    content.raise_for_status()
    return content.content
