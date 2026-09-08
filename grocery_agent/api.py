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

import os
import time
from dataclasses import dataclass
from datetime import date as date_cls

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from grocery_agent import receipt, sandbox, tools
from grocery_agent.db import get_connection
from grocery_agent.llm import generate_reply, parse_utterance
from grocery_agent.notifications import download_telegram_file, send_telegram_message
from grocery_agent.reminders import run_reminder_check
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

# A pending clarification is one bounded record per user — replaced or
# cleared on the next utterance, never a growing transcript.
# Process-local: fine for a single Fly.io instance.
PENDING_CLARIFICATION_TTL_SECONDS = 120
_pending_clarifications: dict[str, "PendingClarification"] = {}

ACTION_TOOLS = {"add_item", "consume_item", "add_to_shopping_list"}
TOOL_FUNCTIONS = {
    "add_item": tools.add_item,
    "consume_item": tools.consume_item,
    "add_to_shopping_list": tools.add_to_shopping_list,
    "query_stock": tools.query_stock,
    "query_item_details": tools.query_item_details,
    "query_shopping_list": tools.query_shopping_list,
    "query_expiring_soon": tools.query_expiring_soon,
}


@dataclass
class PendingClarification:
    original_text: str
    question: str
    asked_at: float


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


def _coerce_arguments(tool_name: str, arguments: dict) -> dict:
    arguments = dict(arguments)
    if tool_name == "add_item":
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


def _run_custom_action(conn, household_id: str, user_id: str, description: str) -> str:
    """The sandboxed code-execution fallback: no predefined tool fit, so
    generate code against the safe /internal/sandbox primitives and run
    it in an isolated Modal Sandbox — never in our own process, never
    with DB credentials. Logged to ActionLog like every other action,
    and the generated code is included so it's inspectable after the
    fact."""
    code = sandbox.generate_code(description)
    sandbox.validate_generated_code(code)
    token = sandbox.issue_sandbox_token(household_id, user_id)
    base_url = os.environ.get("APP_BASE_URL", "http://localhost:8080")
    try:
        result = sandbox.run_in_sandbox(code, base_url, token)
    finally:
        sandbox.revoke_sandbox_token(token)

    ActionLogRepository(conn).log(
        household_id,
        user_id,
        "custom_action",
        {"description": description, "code": code, "result": result},
    )
    return result


def _execute_calls(conn, household_id: str, user_id: str, context: str | None, calls: list) -> str:
    """Runs a list of ToolCalls (from text parsing or receipt parsing)
    and commits once, atomically, at the end. Shared by _process_utterance
    and the receipt-photo path. `context` is the original request text
    (None for a receipt photo) — passed through so the reply can mirror
    its language."""
    facts = []
    custom_action_texts = []
    for call in calls:
        if call.tool_name == "custom_action":
            # Already a finished, LLM-authored message (from the sandbox
            # codegen's own `return` statement) — not re-summarized.
            custom_action_texts.append(
                _run_custom_action(conn, household_id, user_id, call.arguments["description"])
            )
            continue
        arguments = _coerce_arguments(call.tool_name, call.arguments)
        tool_fn = TOOL_FUNCTIONS[call.tool_name]
        if call.tool_name in ACTION_TOOLS:
            result = tool_fn(conn, household_id, user_id, **arguments)
        else:
            result = tool_fn(conn, household_id, **arguments)
        facts.append(_call_to_fact(conn, call.tool_name, arguments, result))
    conn.commit()

    try:
        # Best-effort: a Telegram hiccup shouldn't fail the user's request.
        run_reminder_check(conn, household_id)
        conn.commit()
    except Exception:
        conn.rollback()

    parts = [generate_reply(context, facts)] if facts else []
    parts.extend(custom_action_texts)
    return "\n".join(parts)


def _process_utterance(conn, household_id: str, user_id: str, text: str) -> str:
    """Shared pipeline for a typed message: resolve a pending
    clarification if one's outstanding, parse, then dispatch via
    _execute_calls. Returns the response text."""
    pending = _pending_clarifications.get(user_id)
    is_fresh_pending = (
        pending is not None
        and time.time() - pending.asked_at < PENDING_CLARIFICATION_TTL_SECONDS
    )
    if is_fresh_pending:
        parsed = parse_utterance(
            [
                {"role": "user", "content": pending.original_text},
                {"role": "assistant", "content": pending.question},
                {"role": "user", "content": text},
            ]
        )
    else:
        parsed = parse_utterance(text)

    if parsed.needs_clarification:
        _pending_clarifications[user_id] = PendingClarification(
            original_text=pending.original_text if is_fresh_pending else text,
            question=parsed.clarification or "Could you clarify?",
            asked_at=time.time(),
        )
        _notify_telegram_confirmation(conn, household_id, parsed.clarification)
        return parsed.clarification

    _pending_clarifications.pop(user_id, None)
    response_text = _execute_calls(conn, household_id, user_id, text, parsed.calls)
    _notify_telegram_confirmation(conn, household_id, response_text)
    return response_text


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
# grocery_agent/sandbox.py) via a short-lived, single-use token — never
# by an end-user client directly. This is the entire safe surface area
# generated code can touch: no raw SQL, no shell, no other secrets.


def verify_sandbox_token(
    authorization: str | None = Header(default=None),
) -> sandbox.SandboxTokenScope:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    scope = sandbox.resolve_sandbox_token(authorization.removeprefix("Bearer "))
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


class SandboxConsumeItemRequest(BaseModel):
    name: str
    quantity: float


class SandboxNameRequest(BaseModel):
    name: str


class SandboxEmptyRequest(BaseModel):
    pass


@app.post("/internal/sandbox/get_stock")
def sandbox_get_stock(
    payload: SandboxGetStockRequest, scope: sandbox.SandboxTokenScope = Depends(verify_sandbox_token)
):
    conn = get_connection()
    try:
        return {"items": tools.query_stock(conn, scope.household_id, payload.name)}
    finally:
        conn.close()


@app.post("/internal/sandbox/add_item")
def sandbox_add_item(
    payload: SandboxAddItemRequest, scope: sandbox.SandboxTokenScope = Depends(verify_sandbox_token)
):
    conn = get_connection()
    try:
        purchase_date = (
            date_cls.fromisoformat(payload.purchase_date) if payload.purchase_date else None
        )
        expiry_date = date_cls.fromisoformat(payload.expiry_date) if payload.expiry_date else None
        tools.add_item(
            conn, scope.household_id, scope.user_id, payload.name, payload.quantity,
            purchase_date, expiry_date,
        )
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/internal/sandbox/consume_item")
def sandbox_consume_item(
    payload: SandboxConsumeItemRequest, scope: sandbox.SandboxTokenScope = Depends(verify_sandbox_token)
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
    payload: SandboxNameRequest, scope: sandbox.SandboxTokenScope = Depends(verify_sandbox_token)
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


@app.post("/internal/sandbox/get_shopping_list")
def sandbox_get_shopping_list(
    payload: SandboxEmptyRequest, scope: sandbox.SandboxTokenScope = Depends(verify_sandbox_token)
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
    payload: SandboxEmptyRequest, scope: sandbox.SandboxTokenScope = Depends(verify_sandbox_token)
):
    conn = get_connection()
    try:
        return {"items": tools.query_expiring_soon(conn, scope.household_id)}
    finally:
        conn.close()
