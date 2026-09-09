"""API layer.

FastAPI app exposing POST /utterance (testing/scripting) and
POST /telegram-webhook (the front end: text or a receipt photo, sent
in the household's Telegram group).

Identity comes directly from Telegram (message.from.id/first_name),
not a separate auth system. /utterance is a scripting/testing entry
point authenticated by a static token, mapped to one fixed household —
it is not the production path; Telegram is.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date as date_cls

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from grocery_agent import receipt, sandbox, tools
from grocery_agent.db import get_connection
from grocery_agent.llm import ToolCall, generate_reply, is_affirmative, parse_utterance
from grocery_agent.notifications import download_telegram_file, send_telegram_message
from grocery_agent.reminders import run_reminder_check_for_all_households
from grocery_agent.repositories import (
    ActionLogRepository,
    HouseholdRepository,
    InventoryRepository,
)

load_dotenv()

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/internal/check_reminders")
def check_reminders(authorization: str | None = Header(default=None)):
    """Scheduled entry point for reminder checking — meant to be called
    periodically by an external scheduler (see .github/workflows), not
    triggered by any user request. Reminders are intentionally
    decoupled from request handling (see _finalize) so a household with
    no activity still gets notified about expiring/stale items."""
    secret = os.environ.get("CRON_SECRET")
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="invalid or missing cron secret")

    conn = get_connection()
    try:
        results = run_reminder_check_for_all_households(conn)
        conn.commit()
        return {"households_notified": len(results), "details": results}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# A pending clarification (or custom-action confirmation) is one bounded
# record per user — replaced or cleared on the next utterance, never a
# growing transcript. Process-local: fine for a single Fly.io instance.
PENDING_CLARIFICATION_TTL_SECONDS = 120
_pending_clarifications: dict[str, "PendingClarification"] = {}
_pending_custom_actions: dict[str, "PendingCustomAction"] = {}

# Caps the tool-calling loop (_run_tool_loop) so a model that never stops
# calling tools can't run forever — generous for anything this app does
# (even "clear the shopping list" only needs ~2-3 rounds: list it, then
# remove each item, then stop).
MAX_TOOL_LOOP_ITERATIONS = 8

ACTION_TOOLS = {
    "add_item",
    "consume_item",
    "add_to_shopping_list",
    "remove_from_shopping_list",
    "update_item",
}
TOOL_FUNCTIONS = {
    "add_item": tools.add_item,
    "update_item": tools.update_item,
    "consume_item": tools.consume_item,
    "add_to_shopping_list": tools.add_to_shopping_list,
    "remove_from_shopping_list": tools.remove_from_shopping_list,
    "query_stock": tools.query_stock,
    "query_item_details": tools.query_item_details,
    "query_shopping_list": tools.query_shopping_list,
    "query_expiring_soon": tools.query_expiring_soon,
}


@dataclass
class PendingClarification:
    """Holds the full conversation so far (not just the original text),
    since the tool loop may have already executed some calls before the
    model asked a clarifying question — resuming must continue from
    there, not restart from scratch."""

    messages: list[dict] = field(default_factory=list)
    question: str = ""
    asked_at: float = 0.0


@dataclass
class PendingCustomAction:
    """A custom_action whose generated code has a write effect (see
    sandbox.code_has_write_effect) — held for human confirmation before
    it runs, rather than executed immediately like a read-only
    custom_action or an ordinary tool call. Carries everything needed to
    either run the already-generated code (never regenerated) or
    continue the conversation if declined: the accumulated facts/
    custom_action_texts from earlier in this same turn, so the eventual
    reply covers the whole turn, not just what happens after resuming."""

    messages: list[dict] = field(default_factory=list)
    call_id: str | None = None
    code: str = ""
    description: str = ""
    facts: list[dict] = field(default_factory=list)
    custom_action_texts: list[str] = field(default_factory=list)
    asked_at: float = 0.0


class UtteranceRequest(BaseModel):
    text: str


def verify_api_token(authorization: str | None = Header(default=None)) -> str:
    """/utterance is a testing/scripting entry point, not the production
    path (Telegram is, routed by chat id below) — a static token mapped
    to one fixed household is all it needs. Returns the household id."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ")

    api_token = os.environ.get("API_TOKEN")
    household_id = os.environ.get("API_DEFAULT_HOUSEHOLD_ID")
    if not api_token or not household_id or token != api_token:
        raise HTTPException(status_code=401, detail="invalid token")
    return household_id


def _notify_telegram_confirmation(conn, household_id: str, text: str) -> None:
    """Posts every response (not just reminders) to the household's
    Telegram group, best-effort — lets a client that can only fire a
    one-way webhook (no speech synthesis) read the confirmation there."""
    try:
        household = HouseholdRepository(conn).get(household_id)
        if household and household.telegram_chat_id:
            send_telegram_message(household.telegram_chat_id, text)
    except Exception:
        pass


def _coerce_date_arguments(tool_name: str, arguments: dict) -> dict:
    arguments = dict(arguments)
    if tool_name in ("add_item", "update_item"):
        for date_field in ("purchase_date", "expiry_date"):
            if arguments.get(date_field):
                arguments[date_field] = date_cls.fromisoformat(arguments[date_field])
    return arguments


def _call_to_fact(conn, tool_name: str, arguments: dict, result) -> dict:
    """Resolves what a tool call actually did into a plain, JSON-safe
    fact — only Python has DB access, so this is as far as Python goes.
    Wording/language of the eventual reply is entirely the LLM's job
    (grocery_agent.llm.generate_reply), not decided here."""
    if tool_name in ("add_item", "consume_item", "add_to_shopping_list"):
        fact = {"action": tool_name, **arguments}
        for date_field in ("purchase_date", "expiry_date"):
            if isinstance(fact.get(date_field), date_cls):
                fact[date_field] = fact[date_field].isoformat()
        return fact
    if tool_name == "update_item":
        # Unlike add_item, this can be a no-op (nothing on record for
        # that name) — surface that so the reply doesn't claim a
        # correction that didn't happen.
        if result is None:
            return {"action": tool_name, "name": arguments["name"], "found": False}
        return {
            "action": tool_name,
            "name": arguments["name"],
            "found": True,
            "purchase_date": result.purchase_date.isoformat(),
            "expiry_date": result.expiry_date.isoformat() if result.expiry_date else None,
            "stale_after_days": result.stale_after_days,
        }
    if tool_name == "remove_from_shopping_list":
        # Unlike add_to_shopping_list, this can be a no-op (item wasn't
        # on the list) — surface that so the reply doesn't claim a
        # removal that didn't happen.
        return {"action": tool_name, "name": arguments["name"], "removed": result}
    if tool_name == "query_stock":
        return {"action": tool_name, "requested_name": arguments.get("name"), "items": result}
    if tool_name == "query_item_details":
        return {
            "action": tool_name,
            "name": arguments["name"],
            "items": [
                {
                    "quantity": r["quantity"],
                    "purchase_date": str(r["purchase_date"]),
                    "expiry_date": str(r["expiry_date"]) if r["expiry_date"] else None,
                    "stale_after_days": r["stale_after_days"],
                }
                for r in result
            ],
        }
    if tool_name == "query_shopping_list":
        inventory_repo = InventoryRepository(conn)
        names = [inventory_repo.get_product(e.product_id).name for e in result]
        return {"action": tool_name, "items": names}
    if tool_name == "query_expiring_soon":
        return {
            "action": tool_name,
            "items": [
                {"name": r["name"], "quantity": r["quantity"], "expiry_date": str(r["expiry_date"])}
                for r in result
            ],
        }
    return {"action": tool_name}


def _run_generated_custom_action(
    conn, household_id: str, user_id: str, description: str, code: str
) -> str:
    """Runs already-generated, already-validated code in an isolated
    Modal Sandbox — never in our own process, never with DB credentials
    — and logs it (code included, so it's inspectable after the fact).
    Shared by the immediate path (read-only code) and the confirmed-
    resume path (write-effecting code, run only after a human approved
    it) — the code itself is never regenerated between proposing it and
    running it."""
    callback_token = sandbox.issue_sandbox_callback_token(household_id, user_id)
    base_url = os.environ.get("APP_BASE_URL", "http://localhost:8080")
    try:
        result = sandbox.run_in_sandbox(code, base_url, callback_token)
    finally:
        sandbox.revoke_sandbox_callback_token(callback_token)

    ActionLogRepository(conn).log(
        household_id,
        user_id,
        "custom_action",
        {"description": description, "code": code, "result": result},
    )
    return result


def _run_custom_action(conn, household_id: str, user_id: str, description: str) -> str:
    """The sandboxed code-execution fallback for a request with no
    predefined tool fit — generates and runs code immediately. Only
    reachable for code with no write effect (see
    sandbox.code_has_write_effect); anything that writes must go through
    the confirm-first path in _continue_tool_loop instead. Used by
    _dispatch_call, which in turn is only reached for custom_action via
    the flat executor (_execute_calls, the receipt-photo path) — the
    interactive tool loop handles custom_action itself so it can gate
    on write effect before this ever runs."""
    code = sandbox.generate_action_code(description)
    sandbox.validate_generated_code(code)
    return _run_generated_custom_action(conn, household_id, user_id, description, code)


def _dispatch_call(conn, household_id: str, user_id: str, call: ToolCall) -> tuple[dict, str | None]:
    """Executes one ToolCall for real against the DB (or the sandbox, for
    custom_action). Returns (tool_result, custom_action_text):
    - tool_result is always a plain JSON-safe dict describing what
      happened — usable both as this call's "tool" role result in a
      multi-turn conversation and, for non-custom_action calls, as a
      fact for the final generate_reply call.
    - custom_action_text is set only for custom_action: its sandboxed
      code already returns a finished, LLM-authored confirmation
      message, so that bypasses generate_reply entirely, same as
      before — tool_result just wraps it for the conversation history.
    """
    if call.tool_name == "custom_action":
        text = _run_custom_action(conn, household_id, user_id, call.arguments["description"])
        return {"action": "custom_action", "result": text}, text

    arguments = _coerce_date_arguments(call.tool_name, call.arguments)
    tool_fn = TOOL_FUNCTIONS[call.tool_name]
    if call.tool_name in ACTION_TOOLS:
        result = tool_fn(conn, household_id, user_id, **arguments)
    else:
        result = tool_fn(conn, household_id, **arguments)
    return _call_to_fact(conn, call.tool_name, arguments, result), None


def _finalize(
    conn, household_id: str, context: str | None, facts: list[dict], custom_action_texts: list[str]
) -> str:
    """Commits everything a request did and produces the final reply
    text. Shared tail for both the flat executor (_execute_calls) and
    the interactive tool loop (_run_tool_loop). Reminders are not
    checked here — that's decoupled from request handling entirely and
    runs on a schedule instead (see check_reminders)."""
    conn.commit()
    parts = [generate_reply(context, facts)] if facts else []
    parts.extend(custom_action_texts)
    return "\n".join(parts)


def _execute_calls(conn, household_id: str, user_id: str, context: str | None, calls: list) -> str:
    """Runs a fixed, already-decided list of ToolCalls with no back-and-
    forth — used by the receipt-photo path, which has nothing to
    iterate on (the vision model already extracted the final list of
    items in one shot). `context` is the original request text (None
    for a receipt photo) — passed through so the reply can mirror its
    language. For the interactive text path, see _run_tool_loop."""
    facts = []
    custom_action_texts = []
    for call in calls:
        content, custom_text = _dispatch_call(conn, household_id, user_id, call)
        if custom_text is not None:
            custom_action_texts.append(custom_text)
        else:
            facts.append(content)
    return _finalize(conn, household_id, context, facts, custom_action_texts)


def _build_assistant_tool_calls_message(calls: list[ToolCall]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.tool_name, "arguments": json.dumps(call.arguments)},
            }
            for call in calls
        ],
    }


def _continue_tool_loop(
    conn,
    household_id: str,
    user_id: str,
    messages: list[dict],
    facts: list[dict],
    custom_action_texts: list[str],
    resumed: bool = False,
) -> str | PendingClarification | PendingCustomAction:
    """The actual loop body, factored out so both a fresh request
    (_run_tool_loop) and a resumed custom-action confirmation
    (_resume_custom_action) can drive it. `resumed` distinguishes a
    genuinely fresh loop from one continuing after a confirmation was
    already resolved this turn — a declined confirmation leaves facts/
    custom_action_texts empty (nothing happened) but must still finalize
    with "cancelled" rather than being mistaken for a brand-new,
    nothing's-happened-yet clarification request.

    Each round, the model either calls one or more tools or stops.
    Calls are executed for real and their results fed back as "tool"
    role messages before asking the model again, so it can compose
    requests over existing tools using the real results of its own
    earlier calls in the same turn (e.g. "clear the shopping list" =
    query it, then remove each item) — without needing a sandboxed
    codegen step for something that's just a sequence of calls we
    already support. Capped at MAX_TOOL_LOOP_ITERATIONS rounds so a
    model that never stops calling tools can't loop forever.

    A custom_action call whose generated code would write to the DB
    (sandbox.code_has_write_effect) pauses the whole loop for human
    confirmation instead of running immediately — see PendingCustomAction.

    Returns the final reply text, a PendingClarification if the model
    asked a question before doing any work this turn, or a
    PendingCustomAction if a write-effecting custom_action needs
    confirmation.
    """
    context = messages[0]["content"]

    for iteration in range(MAX_TOOL_LOOP_ITERATIONS):
        parsed = parse_utterance(messages)

        if not parsed.calls:
            if iteration == 0 and not resumed and not facts and not custom_action_texts:
                question = parsed.clarification or "Could you clarify?"
                messages.append({"role": "assistant", "content": question})
                return PendingClarification(messages=messages, question=question, asked_at=time.time())
            break  # model considers the request fully handled

        messages.append(_build_assistant_tool_calls_message(parsed.calls))
        for call in parsed.calls:
            if call.tool_name == "custom_action":
                description = call.arguments["description"]
                code = sandbox.generate_action_code(description)
                sandbox.validate_generated_code(code)
                if sandbox.code_has_write_effect(code):
                    return PendingCustomAction(
                        messages=messages,
                        call_id=call.call_id,
                        code=code,
                        description=description,
                        facts=facts,
                        custom_action_texts=custom_action_texts,
                        asked_at=time.time(),
                    )
                text = _run_generated_custom_action(conn, household_id, user_id, description, code)
                custom_action_texts.append(text)
                content = {"action": "custom_action", "result": text}
            else:
                content, custom_text = _dispatch_call(conn, household_id, user_id, call)
                if custom_text is not None:
                    custom_action_texts.append(custom_text)
                else:
                    facts.append(content)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": json.dumps(content, ensure_ascii=False, default=str),
                }
            )

    return _finalize(conn, household_id, context, facts, custom_action_texts)


def _run_tool_loop(
    conn, household_id: str, user_id: str, messages: list[dict]
) -> str | PendingClarification | PendingCustomAction:
    return _continue_tool_loop(conn, household_id, user_id, messages, facts=[], custom_action_texts=[])


def _resume_custom_action(
    conn, household_id: str, user_id: str, pending: PendingCustomAction, text: str
) -> str | PendingClarification | PendingCustomAction:
    """Resolves a pending write-effecting custom_action: runs the exact
    already-generated code if the reply confirms it, otherwise records
    the decline — either way, the raw reply is also appended as a fresh
    user message so anything else it said (a new request, say) isn't
    silently lost, and the loop continues so the model can react to
    either outcome."""
    confirmed = is_affirmative(text)
    messages = pending.messages
    facts = list(pending.facts)
    custom_action_texts = list(pending.custom_action_texts)

    if confirmed:
        result_text = _run_generated_custom_action(
            conn, household_id, user_id, pending.description, pending.code
        )
        custom_action_texts.append(result_text)
        content = {"action": "custom_action", "result": result_text}
    else:
        content = {"action": "custom_action", "cancelled": True}

    messages.append(
        {
            "role": "tool",
            "tool_call_id": pending.call_id,
            "content": json.dumps(content, ensure_ascii=False, default=str),
        }
    )
    messages.append({"role": "user", "content": text})

    return _continue_tool_loop(
        conn, household_id, user_id, messages, facts, custom_action_texts, resumed=True
    )


def _process_utterance(conn, household_id: str, user_id: str, text: str) -> str:
    """Shared pipeline for a typed message: resumes a pending custom-
    action confirmation or clarification if one's outstanding, then
    runs the interactive tool loop. Returns the response text."""
    pending_action = _pending_custom_actions.pop(user_id, None)
    if pending_action is not None and time.time() - pending_action.asked_at < PENDING_CLARIFICATION_TTL_SECONDS:
        result = _resume_custom_action(conn, household_id, user_id, pending_action, text)
    else:
        pending = _pending_clarifications.get(user_id)
        is_fresh_pending = (
            pending is not None
            and time.time() - pending.asked_at < PENDING_CLARIFICATION_TTL_SECONDS
        )
        messages = (pending.messages if is_fresh_pending else []) + [{"role": "user", "content": text}]
        result = _run_tool_loop(conn, household_id, user_id, messages)

    if isinstance(result, PendingCustomAction):
        _pending_custom_actions[user_id] = result
        question = generate_reply(
            None, [{"action": "confirm_custom_action", "description": result.description}]
        )
        _notify_telegram_confirmation(conn, household_id, question)
        return question

    if isinstance(result, PendingClarification):
        _pending_clarifications[user_id] = result
        _notify_telegram_confirmation(conn, household_id, result.question)
        return result.question

    _pending_clarifications.pop(user_id, None)
    _notify_telegram_confirmation(conn, household_id, result)
    return result


@app.post("/utterance")
def post_utterance(
    payload: UtteranceRequest, household_id: str = Depends(verify_api_token)
):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    conn = get_connection()
    try:
        response_text = _process_utterance(conn, household_id, "api-test", text)
        return {"response": response_text}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/telegram-webhook")
def telegram_webhook(update: dict, request: Request):
    """The primary front end: type a message, or send a receipt photo,
    directly in the household's Telegram group — routed by chat id.
    Registered with Telegram via scripts/set_telegram_webhook.py."""
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and request.headers.get(
        "x-telegram-bot-api-secret-token"
    ) != expected_secret:
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    message = update.get("message") or update.get("edited_message")
    chat_id = message["chat"]["id"] if message else None
    if message is None or chat_id is None:
        return {"ok": True}  # nothing actionable; ack and ignore

    sender = message.get("from") or {}
    user_id = str(sender.get("id", "unknown"))
    if sender.get("first_name"):
        user_id = f"{sender['first_name']} ({user_id})"

    conn = get_connection()
    try:
        household = HouseholdRepository(conn).get_by_telegram_chat_id(chat_id)
        if household is None:
            return {"ok": True}  # unrecognized chat; ack and ignore

        photos = message.get("photo")
        text = message.get("text")
        if photos:
            image_bytes = download_telegram_file(photos[-1]["file_id"])  # largest is last
            calls = receipt.parse_receipt(image_bytes)
            response_text = (
                _execute_calls(conn, household.id, user_id, None, calls)
                if calls
                else generate_reply(None, [{"action": "no_items_found_in_photo"}])
            )
            _notify_telegram_confirmation(conn, household.id, response_text)
        elif text:
            _process_utterance(conn, household.id, user_id, text.strip())
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Internal sandbox API -----------------------------------------------
#
# Called only by code running inside a Modal Sandbox (see
# grocery_agent/sandbox.py) via a short-lived, single-use callback
# token — never by an end-user client directly. This is the entire safe
# surface area generated code can touch: no raw SQL, no shell, no other
# secrets.


def verify_sandbox_callback_token(
    authorization: str | None = Header(default=None),
) -> sandbox.SandboxCallbackScope:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    scope = sandbox.resolve_sandbox_callback_token(authorization.removeprefix("Bearer "))
    if scope is None:
        raise HTTPException(status_code=401, detail="invalid or expired sandbox token")
    return scope


class SandboxGetStockRequest(BaseModel):
    name: str | None = None


class SandboxAddItemRequest(BaseModel):
    name: str
    quantity: float
    purchase_date: str | None = None
    expiry_date: str | None = None
    stale_after_days: int | None = None


class SandboxUpdateItemRequest(BaseModel):
    name: str
    purchase_date: str | None = None
    expiry_date: str | None = None
    stale_after_days: int | None = None


class SandboxConsumeItemRequest(BaseModel):
    name: str
    quantity: float


class SandboxNameRequest(BaseModel):
    name: str


class SandboxEmptyRequest(BaseModel):
    pass


@app.post("/internal/sandbox/get_stock")
def sandbox_get_stock(
    payload: SandboxGetStockRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        return {"items": tools.query_stock(conn, scope.household_id, payload.name)}
    finally:
        conn.close()


@app.post("/internal/sandbox/get_item_details")
def sandbox_get_item_details(
    payload: SandboxNameRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        return {"items": tools.query_item_details(conn, scope.household_id, payload.name)}
    finally:
        conn.close()


@app.post("/internal/sandbox/add_item")
def sandbox_add_item(
    payload: SandboxAddItemRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        purchase_date = (
            date_cls.fromisoformat(payload.purchase_date) if payload.purchase_date else None
        )
        expiry_date = date_cls.fromisoformat(payload.expiry_date) if payload.expiry_date else None
        tools.add_item(
            conn, scope.household_id, scope.user_id, payload.name, payload.quantity,
            purchase_date, expiry_date, payload.stale_after_days,
        )
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/internal/sandbox/update_item")
def sandbox_update_item(
    payload: SandboxUpdateItemRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        purchase_date = (
            date_cls.fromisoformat(payload.purchase_date) if payload.purchase_date else None
        )
        expiry_date = date_cls.fromisoformat(payload.expiry_date) if payload.expiry_date else None
        updated = tools.update_item(
            conn, scope.household_id, scope.user_id, payload.name,
            purchase_date, expiry_date, payload.stale_after_days,
        )
        conn.commit()
        return {"updated": updated is not None}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/internal/sandbox/consume_item")
def sandbox_consume_item(
    payload: SandboxConsumeItemRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        tools.consume_item(conn, scope.household_id, scope.user_id, payload.name, payload.quantity)
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/internal/sandbox/add_to_shopping_list")
def sandbox_add_to_shopping_list(
    payload: SandboxNameRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        tools.add_to_shopping_list(conn, scope.household_id, scope.user_id, payload.name)
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/internal/sandbox/remove_from_shopping_list")
def sandbox_remove_from_shopping_list(
    payload: SandboxNameRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        removed = tools.remove_from_shopping_list(
            conn, scope.household_id, scope.user_id, payload.name
        )
        conn.commit()
        return {"removed": removed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/internal/sandbox/get_shopping_list")
def sandbox_get_shopping_list(
    payload: SandboxEmptyRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        entries = tools.query_shopping_list(conn, scope.household_id)
        inventory_repo = InventoryRepository(conn)
        return {"items": [inventory_repo.get_product(e.product_id).name for e in entries]}
    finally:
        conn.close()


@app.post("/internal/sandbox/get_expiring_soon")
def sandbox_get_expiring_soon(
    payload: SandboxEmptyRequest, scope: sandbox.SandboxCallbackScope = Depends(verify_sandbox_callback_token)
):
    conn = get_connection()
    try:
        return {"items": tools.query_expiring_soon(conn, scope.household_id)}
    finally:
        conn.close()
