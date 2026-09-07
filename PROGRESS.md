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

**Update 2026-09-07 (later still): several real fixes + a language migration.**
- query_stock no longer lists fully-consumed items as "0 eggs" — omits
  them entirely (grocery_agent/tools.py).
- Responses are one item per line, in Chinese (grocery_agent/api.py
  _format_response — item names stay whatever canonical form the DB
  has; unit words translated via _UNIT_ZH).
- **Item names now canonicalize to Chinese, not English** — a
  household consistency request. RESPONSE_LANGUAGE (.env) now drives
  both the clarification language AND the canonical item-name
  language (must be one fixed language, not "mirror input," or the
  same item in two languages would create two DB rows). Migrated all
  existing real item rows to Chinese in one pass (apple->苹果 etc.);
  also fixed pre-existing duplicate "cookie"/"cookies" rows (merged)
  and deleted a stray empty "pingu" row (debug leftover, zero
  batches, harmless). **Real bug caught during this**: RESPONSE_LANGUAGE
  was added to local .env but never pushed as a Fly secret, so the
  deployed app was silently still using English — re-ran
  `fly secrets import` to fix; also cleaned up one duplicate "apple"
  item this gap created before the fix landed.
- System prompt compacted (was 4 verbose paragraphs, now ~5 sentences)
  and now includes today's date, fixing a real bug: "best before
  August 31" was resolving to a date in the past (model had no sense
  of "now").
- Pending-clarification resolution now sends a real 3-turn message
  list (user/assistant/user) to the model instead of one hand-mashed
  string — cleaner and likely more reliable.
- **Real bug: multi-item utterances were silently dropping items.**
  "一块面包，一盒饼干" (bread + cookies) only ever executed the first
  tool call — parse_utterance took message.tool_calls[0] and discarded
  the rest. ParsedCommand now holds a list of ToolCall entries (kept
  .tool_name/.arguments as convenience properties for the common
  single-call case); api.py's _process_utterance loops over all calls
  and commits once at the end.
- **Found while fixing the above — serious model reliability issue**:
  qwen/qwen3.7-flash sometimes returns the *same* tool call repeated
  3-6 times in one response (confirmed reproducible: 5 identical
  requests came back as 1, 6, 2, 1, and 4 calls). This isn't just
  flaky — it silently double/triple-inserted real inventory during
  live testing. Added defensive deduplication in parse_utterance
  (collapses identical (tool, args) pairs) as a safety net regardless
  of model, but the underlying model behavior is the real concern.
  Also seeing qwen3.7-flash's upstream (Alibaba's shared free pool on
  OpenRouter) rate-limit more often in testing (3 failures in one test
  run, vs 1 earlier). Flagged to user for a model-choice decision —
  see chat for the comparison; claude-haiku-4.5 via OpenRouter remains
  the proven-reliable fallback if the user wants to prioritize
  correctness over cost.
- consume_from_batches (services.py) no longer raises when asked to
  consume more than is in stock — floors at zero instead. Over-
  reporting consumption ("I used 6" when 5 were on record) isn't a
  user error, it just means "none left."

**Update 2026-09-07 (later still): unit display simplified to 份 universally**,
per user request ("I think you can use 份 universally") — every item's
display now shows a generic 份 (portion/unit) regardless of its actual
stored unit; lb/kg/pack/etc. stay precise in the DB, this is display-only.
Removed the per-unit Chinese translation table (_UNIT_ZH) from api.py.

**Model choice: deferred, not resolved.** User said "let's deal with
model choice later" — still on qwen/qwen3.7-flash despite the
duplicate-call/rate-limit findings above. Revisit when asked; don't
assume it's been decided either way.

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

## Stage 8 — Sandboxed code-execution fallback (added post-v1)
Status: complete
Completed: 2026-09-07
Notes: grocery_agent/sandbox.py — for requests matching no predefined
tool, a `custom_action` tool (grocery_agent/llm.py TOOLS) triggers a
*second*, separate LLM call (generate_code) that writes a small Python
`def run(ctx): ...` using only a curated primitive set mirroring the
Stage 4 tools (get_stock/add_item/consume_item/add_to_shopping_list/
get_shopping_list/get_expiring_soon) — no raw SQL, no shell, no other
imports (validate_generated_code rejects import/open/exec/eval/
subprocess/os as defense-in-depth on top of sandbox isolation). Runs
inside an isolated Modal Sandbox (real modal.com account, MODAL_TOKEN_ID/
MODAL_TOKEN_SECRET in .env), which calls back into new
/internal/sandbox/* endpoints on our own deployed app via a
short-lived single-use token (grocery_agent.sandbox.issue_sandbox_token)
— the sandbox never holds DB credentials or any other secret. Every
run is logged to ActionLog (action="custom_action", details include
the generated code + result) for auditability.

Real bug caught during live testing (not caught by the unit tests,
which mock the sandbox boundary): the codegen prompt didn't know about
the Chinese item-name canonicalization rule (Section 3a) — it's a
*separate* LLM call from the main parser with its own prompt, so it
doesn't inherit that instruction automatically. First live test
("如果我的梨少于5个，就把梨加到购物清单") generated code using `name="pear"`
(English), which silently failed to match the DB's "梨" row, so the
shopping-list add never happened despite a successful-looking
response. Fixed by adding the same canonicalization instruction to
_CODEGEN_PROMPT, parameterized by RESPONSE_LANGUAGE like the main
prompt. Confirmed working after the fix (generates `ctx.get_stock("梨")`
correctly). Also found: qwen3.7-flash inconsistently routes to
custom_action vs. an existing tool for the same ambiguous phrasing
(2/3 local retries picked query_stock instead) — same underlying
model-reliability question as Stage 5, still deferred per user.

Tests: tests/test_stage8_sandbox.py, 17 tests — validate_generated_code
unit tests, sandbox token round-trip, a real Modal Sandbox execution
(no network), a real codegen LLM call, real DB-backed tests of every
/internal/sandbox/* endpoint via TestClient, and dispatch integration
tests with run_in_sandbox mocked (the actual Modal-to-deployed-app
network round trip isn't practical to test automatically — verified
manually against the live deployed app instead, see above).

Also verified end-to-end against the live deployed app: real codegen +
real Modal sandbox + real callback to https://grocery-agent-koi.fly.dev
+ real DB mutation, confirmed via a follow-up query_shopping_list call.

## Stage 9 — Receipt photo parsing + Siri removal (2026-09-07)
Status: complete
Notes: Per user decision, Siri dropped entirely (docs/siri-shortcut-setup.md
deleted; SIRI_SHORTCUT_TOKEN/SIRI_DEFAULT_USER_ID renamed to generic
API_TOKEN/API_DEFAULT_USER_ID, since /utterance is now just a
testing/scripting entry point — Telegram is the real front end).
grocery_agent/receipt.py: sending a photo to the household's Telegram
group downloads it (notifications.download_telegram_file, Telegram's
two-step file API), sends it to a vision-capable model (VISION_MODEL,
default google/gemini-2.5-flash — qwen3.7-flash isn't vision-capable)
with a prompt mirroring the main parser's canonicalization rule, and
converts the result into add_item ToolCalls dispatched through the
same _execute_calls path as text (refactored out of _process_utterance
so both paths share it). Verified live: real receipt photo → real
vision call → real items added, in Chinese canonical form.

Also fixed two real bugs found via user reports while building this:
- **query_batch_details** (new tool) — "when did I buy X" had no tool
  to answer it at all; query_stock only ever returned an aggregate
  total, never purchase_date. Added a per-batch detail tool exposing
  purchase_date/expiry_date/quantity.
- **Unit-enum crash risk** — the model can return a unit outside
  ALLOWED_UNITS despite the tool schema declaring an enum (observed:
  Chinese measure words "块"/"盒") — Batch's own validation would have
  hard-crashed add_item. _coerce_arguments now falls back to "unit"
  for anything out-of-enum rather than erroring.
- **custom_action over-triggering**: adding that tool made the model
  sometimes route ordinary multi-item requests through it instead of
  multiple add_item calls (4/5 in one local test run). Tightened the
  system prompt: "one call per item/action... custom_action is a LAST
  RESORT ONLY... never use it for something a normal tool call already
  handles." Confirmed fixed (5/5 correct after).

Tests: tests/test_stage9_receipt.py (3, incl. a real vision-model call
against a synthetic fixture receipt), plus new tests in
test_stage4_tools.py (query_batch_details) and test_stage6_api.py
(unit fallback). Full suite: 103 passed.
