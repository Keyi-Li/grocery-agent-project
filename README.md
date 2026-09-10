# Grocery Agent

A Telegram-based grocery inventory tracker. Talk to it in your
household's Telegram group ("bought 2 apples", "used 3 eggs") and it
keeps track of what you have, what's expiring, and what you're running
low on — parsing natural language via an LLM, no
fixed command syntax.

## Usage

### Adding a new household

Add the bot(@ProspectGroceryBot) to your household's
Telegram group and send any message. An unrecognized chat gets walked
through setup automatically: it replies with instructions, and you
reply with the magic word (ask whoever invited you), your household's
name, and optionally a timezone and display language, in your own
words. Each
household gets its own timezone (for when its daily digest arrives)
and its own display language.

### Talking to it

Just talk normally — there's no fixed command syntax. A few examples of what actually happens:

- **Recording a purchase**: "bought 2 apples", "got a dozen eggs
  yesterday".
- **Recording use**: "used 3 eggs", "finished the last 2 apples" — if
  that brings an item to zero, it's automatically added to the
  shopping list.
- **Correcting a record**: "actually I bought the milk 3 days ago" —
  updates the existing entry rather than recording a second purchase.
- **Shopping list**: "add rice to the list", "what's on the shopping
  list" (shows what you added manually vs. what got added
  automatically from running out), "clear the list".
- **Checking stock**: "how much milk do I have", "what's expiring
  soon".
- **Anything else**: a question or request with no fixed command still
  works — e.g. "how many days until the eggs would expire if I'd
  bought them 3 days later" — by generating and running a small
  one-off script instead. If that would actually change your data, it
  asks you to confirm first rather than doing it silently.

### Daily reminders

Once a day, at 6pm in your household's own timezone, you get one
digest message listing anything expiring soon or sitting unused for a
while.

## Under the hood

- **Telegram** is the only front end — one bot, many group chats (one
  per household). 
- **Any OpenAI-compatible LLM endpoint** (`LLM_BASE_URL`) parses each
  request through a multi-turn tool-calling loop — the model can see
  the real result of one call before deciding the next (e.g. "clear
  the shopping list" = look it up, then remove each item), rather than
  guessing everything up front. A separate call phrases the final
  reply, in the same language as the request.
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
   pip install -e .
   ```

   The second line makes `grocery_agent` importable from anywhere —
   without it, running a `scripts/*.py` file directly fails with
   `ModuleNotFoundError: No module named 'grocery_agent'` (only affects
   running scripts locally; the deployed app itself doesn't need this).

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

4. **Create a household and connect Telegram**

   See "Adding a new household" under Usage above — that's enough for
   real use, and needs no script. (Change `MAGIC_WORD` in
   `grocery_agent/api.py` before deploying somewhere others might find
   the bot.) Skip ahead to step 5 unless you also want a household id
   for `/utterance` testing (below), in which case use the manual path
   instead:

   ```bash
   python scripts/setup_household.py --household-name "The Lis" \
     --timezone "America/New_York" --language "English"
   ```

   `--timezone`/`--language` are optional (default `America/New_York`
   and `English`). This prints a household id — put it in `.env` as
   `API_DEFAULT_HOUSEHOLD_ID`. To also connect this household to
   Telegram, get its chat id first — create the group, add the bot,
   send any message, then run `python scripts/get_telegram_chat_id.py`
   — and pass it as `--telegram-chat-id` in the *same* command above;
   the script only ever inserts a new row, so running it a second time
   to attach a chat id creates a duplicate household rather than
   updating the first one.

5. **Run it locally**

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
that never messages the bot should still get notified). Something
external needs to call `POST /internal/check_reminders` (protected by
`CRON_SECRET`) roughly hourly — the endpoint itself only actually
sends a digest to a household when it's currently their configured
local hour, so one shared hourly trigger still lands once a day per
household regardless of timezone. Any hourly scheduler works — a
GitHub Actions workflow on a `schedule: cron` trigger is one
straightforward option if you don't already have a server to run a
cron job on directly.
