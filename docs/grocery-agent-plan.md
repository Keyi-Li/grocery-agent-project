# Grocery Tracker Voice Agent — Build Plan

## 1. Overview

A grocery inventory assistant, used via a household's Telegram group
(text or a receipt photo) to log purchases, log consumption, check
stock, manage a shopping list, and get notified about food expiring
soon. Multiple users can share one or more household inventories.
(Originally planned as voice-first via Siri; superseded post-v1 —
Telegram text + photo turned out to cover the same scenarios without
needing an on-device Shortcut or app. The backend itself is
interface-agnostic, so a voice front end could still be added later
with no backend changes.)

**Core scenarios:**
- "I bought apples: 3 units, pork: 1 pound, milk: 1 unit, best before
  August 31" → logs a new batch per item.
- "I ate one apple" → deducts from stock (oldest-expiring batch first).
- Stock hits 0 → item auto-added to shopping list (tagged `auto`).
- "Add eggs to my list" → manually added to shopping list (tagged
  `manual`).
- "What do I have?" / "What's expiring soon?" → query stock / expiry.
- Anything crossing the 2-day-to-expiry threshold triggers a
  notification (event-driven, not a daily cron).
- Anything unconsumed past its "stale" threshold (default 5 days
  since purchase, configurable per item) also triggers a
  notification, then repeats every 2 days until consumed.
- Users can speak in mixed languages ("milk" one day, "牛奶" the
  next) and the system still recognizes them as the same item.

## 2. Architecture

```
Telegram (text or receipt photo) ─▶ webhook ─▶ FastAPI backend ─▶ OpenRouter (tool-calling / vision)
                                                     │
                                                     ├─▶ Supabase (Postgres + Auth)
                                                     └─▶ Telegram Bot API (reminders +
                                                         confirmations, posted back to
                                                         the household's group)
```

- **Front end is Telegram** — a household's own group chat, with the
  bot as a member. Typed messages go through text parsing
  (`grocery_agent/llm.py`); photo messages (a receipt) go through
  vision parsing (`grocery_agent/receipt.py`) instead. All real logic
  lives server-side; a `POST /utterance` endpoint also exists for
  direct API testing/scripting, auth'd via a static `API_TOKEN`.
- **Backend**: FastAPI (Python), hosted on **Fly.io** (free tier with
  fast wake, so requests still feel responsive).
- **Database + Auth**: **Supabase** free tier (Postgres, relational —
  fits the household/user/inventory/batch relationships naturally;
  free Auth handles multi-user login).
- **NLU**: an LLM via **OpenRouter** (default model: `openai/gpt-4o-mini`,
  configurable via `OPENROUTER_MODEL`), one call per utterance, using
  **tool-calling** so the model both classifies the action and
  extracts structured arguments in a single step (no separate intent
  classifier needed). OpenRouter rather than a direct provider API is
  a deliberate choice here — existing OpenRouter credit, and this
  parsing task is simple enough that model choice isn't critical.
- **Cost**: only the LLM calls are pay-per-use, and at low/personal
  usage volume — plus a cheap model for a task this simple — this is
  on the order of cents/month. Everything else fits comfortably in
  free tiers at this scale.

## 3. Data Model

- **User** — id, email (via Supabase Auth)
- **Household** — id, name, `telegram_chat_id` (nullable — the
  household's Telegram group chat, see Section 3c); many-to-many with
  User (join table `household_members`, no roles — all members have
  equal permissions)
- **Item** — id, name (normalized canonical form — see Section 3a for
  how multilingual input maps here), `stale_after_days` (int,
  default 5; `0` or `-1` means "never remind for staleness"; any
  other positive integer overrides the default for that item, e.g.
  canned goods might be set to `-1`, fresh fish to `2`)
- **Batch** — id, household_id, item_id, quantity (supports
  fractional, e.g. 0.5), unit, purchase_date, expiry_date (nullable)
- **ShoppingListEntry** — id, household_id, item_id, source
  (`auto` | `manual`), created_at. One merged list; `source` explains
  why the item is there.
- **ReminderState** — id, batch_id, reminder_type (`expiry` |
  `staleness`), last_sent_at, active (bool). Tracks whether a
  reminder has already fired for a batch, so repeats are sent on
  schedule rather than on every request (see Section 3b).
- **ActionLog** — id, household_id, user_id, action (e.g.
  `add_item`, `consume_item`), details (JSON — the resolved
  arguments), created_at. Append-only audit trail, written whenever
  a tool function executes. Not read back by the agent itself — it
  exists purely for debugging (e.g. "why does the app think I have 3
  apples?") and as a foundation for an optional future "recent
  activity" view. See Section 3d.

Each purchase event creates a new **Batch** row — batches of the same
item are **not** merged, so different expiry dates/purchase dates
stay distinguishable (this is what makes FEFO and per-batch staleness
checks possible).

**Consumption logic (FEFO — First-Expire-First-Out):** when consuming
quantity Q of an item, deduct from the batch with the soonest
`expiry_date` first, spilling into the next-soonest batch if that one
is exhausted. When an item's total quantity across all batches hits
0, auto-insert a `ShoppingListEntry` with `source=auto` (skip if a
`manual` entry for that item already exists, to avoid duplicates).
Consuming a batch to 0 also deactivates any `ReminderState` rows tied
to it (see 3b).

### 3a. Multilingual item normalization

Users may say "milk" one day and "牛奶" the next and expect both to
resolve to the same inventory item. This is handled **at the LLM
parsing stage (Stage 5), not via a lookup table**: the prompt
instructs Claude to always output the item name normalized to a
single canonical form (e.g. always English, lowercase, singular)
regardless of what language or phrasing the user spoke in. The
database only ever stores/matches on this canonical form, so no
alias table needs to be built or maintained — the normalization
generalizes to any language Claude understands.

### 3b. Expiry vs. staleness reminders

Two independent reminder triggers exist per batch, checked
opportunistically on every backend request (no cron needed for v1):

- **Expiry reminder** — fires once when a batch's `expiry_date` comes
  within 2 days. **One-time only** — does not repeat.
- **Staleness reminder** — fires once when a batch has gone
  unconsumed for `stale_after_days` since `purchase_date` (default
  5, configurable per item, `0`/`-1` disables it for that item).
  Unlike expiry, this one **repeats every 2 days** for as long as the
  batch remains unconsumed, then stops automatically once the batch
  is fully consumed.

The `ReminderState` table (Section 3) is what makes the repeat
behavior correct: each check compares `now` against
`last_sent_at + 2 days` (for staleness) rather than re-firing on
every single request, and `active` is flipped off on consumption so
no further reminders are sent for that batch.

### 3c. Notification targeting via Telegram

Reminders are delivered by posting a message to the household's
**Telegram group** via the Telegram Bot API, rather than through
per-device push tokens. This sidesteps device registration entirely:
anyone in the group — on any device they have Telegram installed on —
sees the notification, with no sign-in flow, no notification-
permission prompt, and no companion app needed.

Setup is a one-time, per-household bootstrap rather than a per-device
flow:
1. Create a bot once via Telegram's `@BotFather` → get a bot token
   (`TELEGRAM_BOT_TOKEN`, a single global secret in `.env`, not
   per-household).
2. For each household, create a Telegram group, add the bot to it,
   and send any message in the group.
3. Look up the resulting chat id (via the bot's `getUpdates` call) and
   store it as that household's `telegram_chat_id` (Section 3).

When a reminder fires for a batch belonging to household 42, the
backend posts to the Telegram Bot API using `TELEGRAM_BOT_TOKEN` and
household 42's `telegram_chat_id` — one API call notifies every member
of the group. Adding or removing a household member is just adding or
removing them from the Telegram group; there's no separate
registration table to keep in sync.

### 3d. Why no conversation history is needed

This agent is stateless per request, apart from one small exception.
Every fact worth remembering — current stock, shopping list,
consumption — already lives in Postgres, so there is no need to
replay a growing chat transcript back to Claude on each call: the
database is the memory, not the conversation log. Each utterance is
handled independently (transcript in → one tool call out → DB
updated → response out), unlike a typical chatbot where compaction of
a long conversation matters because the LLM's only memory of earlier
turns *is* the conversation itself.

The one genuine piece of cross-request state is the short-lived
**pending clarification** (Section 4) — a small structured flag
("waiting on: quantity, for: consume_item(apple)"), not a transcript.
It doesn't grow and needs no summarization; it's cleared the instant
it's resolved or after a short timeout.

`ActionLog` (Section 3) is separate from this — it is a one-way audit
trail for human debugging, not something the agent reads back to
decide what to do next.

## 4. LLM Tool Definitions

The OpenRouter call (Section 2) is given these tools; it picks one per
utterance and fills the arguments:

- `add_item(name, quantity, unit, expiry_date?)`
- `consume_item(name, quantity)`
- `add_to_shopping_list(name)` — always tagged `manual`
- `query_stock(name?)` — specific item or full inventory
- `query_shopping_list()`
- `query_expiring_soon()`

All `name` arguments are normalized by Claude to a single canonical
form at parse time (Section 3a), regardless of the input language —
this is a prompt-design requirement for Stage 5, not separate logic.

**Ambiguity handling:** if the model can't confidently fill required
arguments (e.g. "consume apple" with no quantity), it returns a
clarifying question instead of a tool call. The backend holds a
short-lived "pending clarification" state per user session so the
next utterance is interpreted as the answer to that question, not a
fresh command.

## 5. Build Order (bottom-up, testable checkpoints)

This order mirrors a standard agent-building workflow: domain → logic
→ data → tools → LLM orchestration → API → external interface. The
LLM sits on top of ordinary business logic rather than being the
foundation — a useful pattern to internalize since it generalizes to
most agent projects, not just this one.

At each stage: write the code, write pytest tests for it, run and
review before moving to the next stage.

### Stage 1 — Domain models
Plain Python classes, no framework, no DB: `Item`, `Batch`,
`Household`, `User`, `ShoppingListEntry`. Include validation (e.g.
quantity can't go negative, unit must be from an allowed set).
**Tests:** construct objects, check validation rules raise/pass
correctly. Pure and fast — no I/O.

### Stage 2 — Core business logic / services
Pure functions/classes operating on Stage-1 objects held in memory:
`consume_from_batches` (FEFO logic), `is_expiring_soon`,
`maybe_add_to_shopping_list`. No DB yet.
**Tests:** build in-memory lists of batches, assert consumption
deducts from the correct batch, assert shopping-list logic fires (or
doesn't) in the right conditions.

### Stage 3 — Persistence layer
Repository classes wrapping Supabase (`InventoryRepository`,
`ShoppingListRepository`, `HouseholdRepository`) that translate
between domain objects and DB rows.
**Tests:** run against a local Postgres (Docker) or a Supabase test
project; verify round-trip create/read/update/delete and that FEFO
ordering survives a real query.

### Stage 4 — Tool functions
Thin wrapper functions matching the LLM tool signatures from Section
4, each calling into the Stage-2/3 services, then writing a row to
`ActionLog` recording what happened. No LLM involved yet — these are
just callable Python functions.
**Tests:** call each tool function directly with fixed arguments,
assert correct DB state after, and assert an `ActionLog` row was
written with the expected action/details.

### Stage 5 — LLM parsing layer
Prompt design + an OpenRouter tool-calling call wired to the Stage-4
functions' schemas. Turns raw utterance text into a selected tool +
arguments (or a clarifying question).
**Tests:** a fixed set of sample utterances (including ambiguous
ones) checked against expected tool name + arguments.

### Stage 6 — API layer
FastAPI endpoints (e.g. `POST /utterance`) exposing "send text, get
spoken-back response" to the outside world. Handles auth (Supabase
token) and the pending-clarification session state.
**Tests:** `TestClient`/`httpx` hitting the endpoint, asserting
correct response text and DB side effects.

### Stage 7 — Telegram integration + notifications
Superseded plan: originally a Siri Shortcut (capture dictation → POST
→ speak response), dropped post-v1 in favor of Telegram text/photo —
simpler (no on-device Shortcut to build), and the backend was already
interface-agnostic so nothing else had to change.

Wire up the Telegram side (Section 3c): create the bot via
`@BotFather`, add it to each household's group, and capture each
group's `telegram_chat_id` into the `Household` row. No companion app
is needed — this is a few one-time manual/admin steps, not a user-
facing flow.

Manual end-to-end test, since this layer lives outside the Python
test harness: confirm a shared household item triggers a message in
the household's Telegram group, visible to every member of that
group.

### Stage 8 — Sandboxed code-execution fallback

Added after v1: rather than hand-writing a new tool for every gap
found in real use (e.g. "clear all my stock"), the LLM has a
`custom_action` tool as a last resort. It triggers a **second**,
separate LLM call (`grocery_agent.sandbox.generate_code`) that writes
a small Python function against a narrow, curated set of primitives
(mirroring the Stage 4 tools — get/add/consume stock, shopping list
ops) — never raw SQL, never the shell, never other secrets. That
function runs inside an isolated **Modal Sandbox**, not our own
process; the sandbox calls back into `/internal/sandbox/*` endpoints
on our own deployed app via a short-lived, single-use token, so it
never holds DB credentials itself. Every generated snippet is logged
to `ActionLog` (audit trail) and its result reaches the user like any
other response — this is "compose existing primitives in new ways,"
not "let the model do anything."

Caveat found in practice: the codegen call must independently be told
about item-name canonicalization (Section 3a) — it doesn't inherit
that instruction from the main parser automatically, since it's a
separate LLM call with its own prompt (`grocery_agent/sandbox.py`,
`_CODEGEN_PROMPT`).

## 6. Open Items for Later (v2+)
- Scheduled (non-request-triggered) expiry/staleness check, for
  catching thresholds on days with no Telegram activity (v1 only
  checks opportunistically when the backend is touched).
- A voice front end (Siri Shortcut or similar) — dropped from the plan
  post-v1 in favor of Telegram, but the backend is interface-agnostic
  so this could be re-added without backend changes if wanted later.
- Smarter LLM features beyond parsing (recipe suggestions from stock,
  waste-reduction tips) — explicitly deferred per current scope.
