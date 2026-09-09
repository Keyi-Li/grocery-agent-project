# Grocery Agent

A Telegram-based grocery inventory tracker. Send it a message ("bought
2 apples", "used the milk") or a photo of a receipt in your household's
Telegram group, and it keeps track of what you have, what's expiring,
and what you're running low on — parsing natural language (English or
Chinese) via an LLM, no fixed command syntax.

## How it works

- **Telegram** is the only front end — one bot, one group chat per
  household. Text and receipt photos both go through the same webhook.
- **Any OpenAI-compatible LLM endpoint** (`LLM_BASE_URL`) parses each
  request through a multi-turn tool-calling loop — the model can see
  the real result of one call before deciding the next (e.g. "clear
  the shopping list" = look it up, then remove each item), rather than
  guessing everything up front. A separate call phrases the final
  reply, in the same language as the request. Receipt photos go
  through a vision-capable model instead.
- **A sandboxed fallback** (Modal) handles requests no predefined tool
  covers: a small generated Python script runs in an isolated sandbox
  with no DB credentials, calling back into a narrow internal API.
  Anything the generated code would actually write to the database
  pauses for a human "confirm?" in Telegram before it runs — read-only
  requests run immediately.
- **Postgres (via Supabase)** is the datastore — plain hosted Postgres,
  no Supabase Auth; identity comes directly from Telegram.
- **Scheduled reminders**: each household has its own timezone: once a
  day, at 6pm in *their* local time, they get one digest message
  listing anything expiring soon or gone untouched for a while —
  decoupled from whether anyone's actually used the bot that day.
- **Fly.io** hosts the FastAPI app.

## Prerequisites

- Python 3.11+
- A [Supabase](https://supabase.com) project (free tier is fine)
- A [Telegram bot](https://core.telegram.org/bots#botfather) token from `@BotFather`
- An API key for an OpenAI-compatible LLM endpoint — [OpenRouter](https://openrouter.ai/keys)
  is the simplest starting point; any compatible endpoint works
- A [Modal](https://modal.com) account and API token
- A [Fly.io](https://fly.io) account, for deployment
- A GitHub Actions-enabled fork/clone of this repo, for scheduled reminders

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
   where to get it. `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` are
   all required (no fallback if left blank). `API_DEFAULT_HOUSEHOLD_ID`
   is filled in during step 4, so leave it blank for now.

3. **Create the database schema**

   Apply `grocery_agent/schema.sql` to your Supabase project (it's
   idempotent — safe to re-run). Simplest way, via `psql`:

   ```bash
   psql "$DIRECT_URL" -f grocery_agent/schema.sql
   ```

4. **Create a household**

   ```bash
   python scripts/setup_household.py --household-name "The Lis" \
     --timezone "America/New_York"
   ```

   `--timezone` is optional (an IANA name; defaults to
   `America/New_York`) — it's what "6pm" means for that household's
   daily reminder digest. This prints a household id — put it in
   `.env` as `API_DEFAULT_HOUSEHOLD_ID`.

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

### Scheduled reminders

Reminders are intentionally decoupled from user requests (a household
that never messages the bot should still get notified). A GitHub
Actions workflow (`.github/workflows/check-reminders.yml`) calls
`POST /internal/check_reminders` every hour; the endpoint itself only
actually sends a digest to a household when it's currently their
configured local hour, so one shared hourly trigger still lands once a
day per household regardless of timezone. To activate it:

1. Push this repo to GitHub (the workflow only runs once it's on the
   default branch).
2. Add `CRON_SECRET` (the same value as in your `.env`/Fly secrets) as
   a GitHub Actions repository secret: **Settings → Secrets and
   variables → Actions → New repository secret**.
