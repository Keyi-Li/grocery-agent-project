"""One-time (or re-run-after-redeploy) setup: tells Telegram to POST
incoming messages to our deployed /telegram-webhook endpoint.

Usage: python scripts/set_telegram_webhook.py https://your-app.fly.dev
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/set_telegram_webhook.py <base-url>")
    base_url = sys.argv[1].rstrip("/")

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]

    response = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={
            "url": f"{base_url}/telegram-webhook",
            "secret_token": secret,
        },
        timeout=10,
    )
    response.raise_for_status()
    print(response.json())


if __name__ == "__main__":
    main()
