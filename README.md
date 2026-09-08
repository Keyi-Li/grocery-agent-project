# Grocery Agent

A Telegram-based grocery inventory tracker. Send it a message ("bought
2 apples", "used the milk") or a photo of a receipt in your household's
Telegram group, and it keeps track of what you have, what's expiring,
and what you're running low on — parsing natural language (English or
Chinese) via an LLM, no fixed command syntax.

## How it works

- **Telegram** is the only front end — one bot, one group chat per
  household. Text and receipt photos both go through the same webhook.
- **OpenRouter** provides the LLM: one call parses an utterance into
  tool calls (add/consume/query), a second turns the result into a
  reply in the same language as the request. Receipt photos go through
  a vision-capable model instead.
- **Postgres (via Supabase)** is the datastore — plain hosted Postgres,
  no Supabase Auth; identity comes directly from Telegram.
- **Modal** runs a sandboxed fallback for requests no predefined tool
  covers (e.g. "clear all my stock"): a small generated Python script
  runs in an isolated sandbox with no DB credentials, calling back into
  a narrow internal API.
- **Fly.io** hosts the FastAPI app.

## Prerequisites

- Python 3.11+
- A [Supabase](https://supabase.com) project (free tier is fine)
- A [Telegram bot](https://core.telegram.org/bots#botfather) token from `@BotFather`
- An [OpenRouter](https://openrouter.ai/keys) API key
- A [Modal](https://modal.com) account and API token
- A [Fly.io](https://fly.io) account, for deployment

## Setup

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment**

   ```bash
   cp .env.example .env
   ```

   Fill in every value in `.env` — each one has a comment explaining
   where to get it. `API_DEFAULT_HOUSEHOLD_ID` is filled in during step
   4, so leave it blank for now.

3. **Create the database schema**

   Apply `grocery_agent/schema.sql` to your Supabase project (it's
   idempotent — safe to re-run). Simplest way, via `psql`:

   ```bash
   psql "$DIRECT_URL" -f grocery_agent/schema.sql
   ```

4. **Create a household**

   ```bash
   python scripts/setup_household.py --household-name "The Lis"
   ```

   This prints a household id — put it in `.env` as
   `API_DEFAULT_HOUSEHOLD_ID`.

5. **Connect Telegram**

   - Create your household's Telegram group and add the bot to it.
   - Send any message in the group, then run:

     ```bash
     python scripts/get_telegram_chat_id.py
     ```

     to find its chat id.
   - Save that chat id on the household row (re-run
     `setup_household.py` with `--telegram-chat-id`, or update the row
     directly).

6. **Run it locally**

   ```bash
   uvicorn grocery_agent.api:app --reload --port 8080
   ```

   You can exercise the logic without Telegram via `POST /utterance`
   (authenticated with `API_TOKEN` from `.env`):

   ```bash
   curl -X POST localhost:8080/utterance \
     -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"text": "bought 2 apples"}'
   ```

## Deploying

The app is set up to deploy to Fly.io (see `fly.toml`, `Dockerfile`).

```bash
fly launch   # first time only — or fly deploy if the app already exists
fly secrets import < .env
python scripts/set_telegram_webhook.py https://<your-app>.fly.dev
```

After that, messages sent in the household's Telegram group are
delivered to `/telegram-webhook` on your deployed app.

## Tests

```bash
pytest
```

Most tests run against the real Supabase project in `.env` (each test
rolls back its own transaction) and the real OpenRouter API, so
`.env` must be fully configured before running the suite.
