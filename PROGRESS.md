# Build Progress

Plan: docs/grocery-agent-plan.md

## Stage 1 — Domain models
Status: complete
Completed: 2026-09-06
Notes: Implemented in grocery_agent/dataclass.py (renamed from models.py
per user preference) as plain @dataclass classes: User, Household,
Item, Batch, ShoppingListEntry. Validation via __post_init__ raising
ValueError. Deviation from plan: Batch.unit's "allowed set" was not
specified in the plan — resolved with user as {unit, lb, oz, kg, g, l,
ml, pack}, defaulting to "unit". Household.member_ids is a list of
ids (not embedded User objects) to mirror the future household_members
join table. IDs are auto-generated UUID strings but overridable.
Tests: tests/test_stage1_domain.py, 20 tests, all passing.

## Stage 2 — Core business logic / services
Status: complete
Completed: 2026-09-06
Notes: grocery_agent/services.py — consume_from_batches (FEFO, atomic:
raises ValueError without mutating anything if insufficient stock),
is_expiring_soon (true within 2 days of expiry or already past it),
is_stale (respects Item.stale_after_days 0/-1 disable, false once a
batch is fully consumed), maybe_add_to_shopping_list (fires only when
an item's total qty hits 0 and no existing auto/manual entry exists).
Pure functions, no DB. Tests: tests/test_stage2_services.py, 18 tests.

## Stage 3 — Persistence layer
Status: complete
Completed: 2026-09-06
Notes: grocery_agent/db.py (connections: DATABASE_URL/transaction
pooler for app queries, DIRECT_URL/session pooler for schema),
grocery_agent/schema.sql (users, households, household_members, items,
batches, shopping_list_entries, reminder_state, action_log — all
CREATE TABLE IF NOT EXISTS), grocery_agent/repositories.py
(UserRepository, HouseholdRepository, InventoryRepository,
ShoppingListRepository). Deviations: added Household.telegram_chat_id
to the Stage 1 dataclass (needed for the Telegram-group notification
design decided after Stage 1 shipped); added UserRepository (not named
in the plan's Stage 3 list, but needed for FK integrity). Tests: run
against the real Supabase project via tests/conftest.py — schema
applied once per session, each test's writes rolled back after.
tests/test_stage3_persistence.py, 7 tests, including a real-query FEFO
ordering check.

## Stage 4 — Tool functions
Status: complete
Completed: 2026-09-06
Notes: grocery_agent/tools.py — add_item, consume_item,
add_to_shopping_list, query_stock, query_shopping_list,
query_expiring_soon. Each wraps Stage 2/3 and writes an ActionLog row
(new ActionLogRepository added to repositories.py). query_stock
aggregates a household's batches per item; if an item has batches in
more than one unit (edge case, not addressed in the plan), reports
unit "mixed" rather than guessing. Tests: tests/test_stage4_tools.py,
6 tests, against the real Supabase DB (rolled back after).

## Stage 5 — LLM parsing layer
Status: complete
Completed: 2026-09-06
Notes: grocery_agent/llm.py — parse_utterance() calls an LLM via
**OpenRouter** (default model openai/gpt-4o-mini, overridable via
OPENROUTER_MODEL) using OpenAI-style tool-calling, wired to the Stage 4
tool schemas; returns ParsedCommand(tool_name, arguments) or, if
unable to confidently fill a required argument,
ParsedCommand(tool_name=None, clarification=...). Deviation from plan:
switched from Claude to OpenRouter per user's explicit request (they
already hold OpenRouter credit; task judged simple enough that
provider/model choice isn't critical) — originally built against
Claude directly, kept working via ANTHROPIC_API_KEY (still in .env,
unused) until this switch.
Found a real quality gap from picking gpt-4o-mini specifically:
initially failed to extract quantity from spelled-out Chinese numerals
("一个苹果" = "one apple") while handling digit form ("1个苹果") and
English ("one apple") fine — fixed by making the system prompt
explicitly call out that spelled-out numbers (any language, incl.
Chinese numerals) must resolve to a plain number, not trigger a
clarifying question. Worth knowing if switching to an even cheaper/
smaller model later: re-check this exact case.
Tests: tests/test_stage5_llm.py, 9 tests against the real OpenRouter
API, all passing.

**Update 2026-09-07 (later): reverted to qwen/qwen3.7-flash** — user's
call, claude-haiku-4.5 was judged too expensive despite being more
reliable (absolute cost is still trivial at personal usage volume, but
this was an explicit preference, not a technical necessity). If
reliability issues resurface in real use, claude-haiku-4.5 via
OpenRouter is the known-good fallback — see the comparison below.

Update 2026-09-07 (earlier, superseded by revert above): switched to anthropic/claude-haiku-4.5 (via
OpenRouter, still — user wanted to keep OpenRouter credit/billing,
just picked a stronger model). Reason: qwen3.7-flash turned out
unreliable in real use beyond what the test suite caught — occasional
malformed non-tool-calling output ("call: default_api:query_stock{...}"
as plain text) and highly variable latency (2-9s). Benchmarked
claude-haiku-4.5 head-to-head: 8/8 correct across repeated trials,
consistently fast (0.7-1.5s, in gpt-4o-mini's range), full
tests/test_stage5_llm.py suite passes in ~10s (vs qwen's ~33s+).
OPENROUTER_MODEL updated locally and as a Fly secret; redeployed and
verified live ("consume apple" correctly asks "How many apples would
you like to consume?" instead of guessing). This is now the
recommended default going forward unless cost becomes a real concern
at much higher volume than personal use.

Original model decision (2026-09-06, superseded above): compared several cheap OpenRouter models
by real pricing + live latency + a quick correctness check, given the
household's actual bilingual English/Chinese use. Settled on
**qwen/qwen3.7-flash** ($0.03/$0.13 per 1M in/out — cheapest of the
bunch, native-Chinese model) over openai/gpt-4o-mini (the original
pick) and google/gemini-2.5-flash-lite, despite qwen3.7-flash being
noticeably slower (2-5s per call vs gpt-4o-mini's 0.5-0.8s) — user's
explicit call after seeing the latency numbers. Also benchmarked
qwen/qwen-2.5-72b-instruct as a candidate: same latency as
qwen3.7-flash (no speed win) AND a real correctness regression — it
silently guessed quantity=1 for "consume apple" (no stated quantity)
instead of asking for clarification, which qwen3.7-flash and
gpt-4o-mini both get right. Ruled out. OPENROUTER_MODEL is set to
qwen/qwen3.7-flash both locally (.env) and as a Fly secret; redeployed
and verified live.

## Stage 6 — API layer
Status: complete
Completed: 2026-09-06
Notes: grocery_agent/api.py — FastAPI app, POST /utterance (+ GET
/health). Auth: verify_supabase_token supports (1) a static
SIRI_SHORTCUT_TOKEN mapped to SIRI_DEFAULT_USER_ID for the Siri
Shortcut client (see Stage 7 notes — a plain Shortcut can't run a
Supabase token-refresh flow), falling back to (2) real Supabase Auth
token verification via /auth/v1/user for any future proper client.
Pending-clarification state: one bounded record per user_id
(original_text, question, asked_at), replaced/cleared each turn, not a
growing transcript, matching plan Section 3d; process-local dict
(fine for a single Fly.io instance, noted as a scope decision).
Tests: tests/test_stage6_api.py, 7 tests — the LLM parse step is
monkeypatched with fixed results (Stage 5 already tests real model
behavior separately), auth is overridden via FastAPI
dependency_overrides; DB/tool functions/services underneath are real.

## Stage 7 — Voice integration + Telegram notifications
Status: backend deployed and verified live; two manual on-device/on-Telegram steps remain
Completed: 2026-09-06
Notes: grocery_agent/notifications.py (send_telegram_message),
grocery_agent/reminders.py (run_reminder_check — checks every batch
against Stage 2's is_expiring_soon/is_stale on each /utterance
request, using ReminderStateRepository to send expiry once and
staleness every 2 days, wired into api.py after each action).
tools.consume_item deactivates a batch's reminder_state when it hits 0
(plan Section 3). scripts/get_telegram_chat_id.py and
scripts/setup_household.py handle one-time setup.
Tests: tests/test_stage7_reminders.py, 6 tests — send_telegram_message
mocked at the HTTP boundary, everything else real.

**Deployed**: Dockerfile + requirements.txt + fly.toml
(shared-cpu-1x/256mb, auto_stop/auto_start so it scales to zero when
idle, health check on GET /health). Live at
https://grocery-agent-koi.fly.dev. Both Fly.io blockers (needed a card
on file, which you added) are resolved.

**Real account wired up**: created your Supabase Auth user
(yalephd2022@gmail.com, temporary password set via the Admin API —
change it in the Supabase dashboard whenever), ran setup_household.py
to create "Koi's Household" and
link it to that user (id fd945448-ec3e-497d-9068-a880259cd97d), set
SIRI_DEFAULT_USER_ID as both a local .env value and a Fly secret.
Verified live end-to-end against the deployed app: "I bought 3 apples"
-> "Added 3 apples.", "what do I have" -> "2 eggs; 3 apples" (real
Supabase rows, real OpenRouter parse). Also fixed spoken-response
grammar in api.py (_format_quantity/_pluralize_*): "2.0 unit of apple"
-> "2 apples" — minor but this is literally what Siri speaks aloud.

**Telegram: done (2026-09-07).** Group created ("yinchen, Keyi and
GroceryAgent"), bot added, chat id fetched via
scripts/get_telegram_chat_id.py. Note: Telegram upgraded the group
from a basic group to a supergroup mid-setup, which changes its chat
id (a `-100...`-prefixed id replaces the old one) — used the
supergroup id. Household "Koi's Household"
(id 38783806-105b-4a2f-9df6-191ed72bb763) now has
telegram_chat_id=-1004444253597, confirmed via a real test message
delivered to the group. run_reminder_check (wired into every
/utterance call) will now actually deliver expiry/staleness reminders
for this household instead of no-op'ing.

**One thing left, needs you (not automatable from here)**:
Siri Shortcut — manual on-device build, steps in
docs/siri-shortcut-setup.md. Use https://grocery-agent-koi.fly.dev
as the URL and SIRI_SHORTCUT_TOKEN from .env as the bearer token.
