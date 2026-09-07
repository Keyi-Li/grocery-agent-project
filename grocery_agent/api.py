"""Stage 6 — API layer.

FastAPI app exposing POST /utterance: send text, get a spoken-back
response back. Handles Supabase-token auth and the short-lived
pending-clarification session state. See docs/grocery-agent-plan.md
Section 2 (API), Section 3d and Section 4 (pending clarification).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date as date_cls

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from grocery_agent import receipt, sandbox, tools
from grocery_agent.dataclass import ALLOWED_UNITS
from grocery_agent.db import get_connection
from grocery_agent.llm import parse_utterance
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
# cleared on the next utterance, never a growing transcript (plan
# Section 3d). Process-local: fine for a single Fly.io instance.
PENDING_CLARIFICATION_TTL_SECONDS = 120
_pending_clarifications: dict[str, "PendingClarification"] = {}

ACTION_TOOLS = {"add_item", "consume_item", "add_to_shopping_list"}
TOOL_FUNCTIONS = {
    "add_item": tools.add_item,
    "consume_item": tools.consume_item,
    "add_to_shopping_list": tools.add_to_shopping_list,
    "query_stock": tools.query_stock,
    "query_batch_details": tools.query_batch_details,
    "query_shopping_list": tools.query_shopping_list,
    "query_expiring_soon": tools.query_expiring_soon,
}


@dataclass
class PendingClarification:
    original_text: str
    question: str
    asked_at: float


@dataclass
class AuthenticatedUser:
    id: str
    email: str


class UtteranceRequest(BaseModel):
    text: str


def verify_supabase_token(
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    """Validates the caller's bearer token one of two ways:

    1. A static API_TOKEN, mapped directly to API_DEFAULT_USER_ID (set
       by scripts/setup_household.py) — for testing/scripting against
       /utterance directly. The primary client (Telegram) doesn't use
       this path at all; it's routed by chat id instead.
    2. Otherwise, a real Supabase Auth access token, verified by
       asking Supabase's own Auth server whose token it is. This path
       is what any future real client (a proper mobile app, a web UI)
       would use instead.

    Overridden in tests via FastAPI dependency_overrides so tests
    don't need a live Supabase Auth session."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ")

    api_token = os.environ.get("API_TOKEN")
    api_user_id = os.environ.get("API_DEFAULT_USER_ID")
    if api_token and api_user_id and token == api_token:
        return AuthenticatedUser(id=api_user_id, email="")

    response = httpx.get(
        f"{os.environ['SUPABASE_URL']}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": os.environ["SUPABASE_ANON_KEY"],
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid token")
    data = response.json()
    return AuthenticatedUser(id=data["id"], email=data.get("email", ""))


def _get_household_id(conn, user_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT household_id FROM household_members WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="user belongs to no household")
    return row[0]


def _notify_telegram_confirmation(conn, household_id: str, text: str) -> None:
    """Posts every /utterance response (not just reminders) to the
    household's Telegram group, best-effort. This lets a non-Siri
    client (e.g. an Android IFTTT/Assistant routine that can fire a
    one-way webhook but can't speak a response back) read the
    confirmation in Telegram instead."""
    try:
        household = HouseholdRepository(conn).get(household_id)
        if household and household.telegram_chat_id:
            send_telegram_message(household.telegram_chat_id, text)
    except Exception:
        pass


def _coerce_arguments(tool_name: str, arguments: dict) -> dict:
    arguments = dict(arguments)
    if tool_name == "add_item":
        if arguments.get("expiry_date"):
            arguments["expiry_date"] = date_cls.fromisoformat(arguments["expiry_date"])
        if arguments.get("unit") not in ALLOWED_UNITS:
            # The tool schema declares an enum, but not every model
            # actually enforces it (seen: "块"/"盒", Chinese measure
            # words) — Batch's own validation would hard-crash on an
            # out-of-enum unit, so fall back rather than error out.
            arguments["unit"] = "unit"
    return arguments


def _format_quantity(quantity: float) -> str:
    return str(int(quantity)) if quantity == int(quantity) else str(quantity)


def _item_line(name: str, quantity: float) -> str:
    """份 (a generic "portion/unit") is used for every item's display,
    regardless of its actual stored unit (lb/kg/pack/etc. stay precise
    in the DB — this is a display simplification, not a storage one)."""
    return f"{_format_quantity(quantity)} 份 {name}"


def _format_response(conn, tool_name: str, arguments: dict, result) -> str:
    """All deterministic (non-LLM-generated) responses are in Chinese,
    regardless of what language the utterance was in — item names stay
    in their canonical form from the DB (Section 3a), not translated
    per response."""
    if tool_name == "add_item":
        return f"已添加 {_item_line(arguments['name'], arguments['quantity'])}。"
    if tool_name == "consume_item":
        return f"已记录消耗 {_item_line(arguments['name'], arguments['quantity'])}。"
    if tool_name == "add_to_shopping_list":
        return f"已将 {arguments['name']} 加入购物清单。"
    if tool_name == "query_stock":
        if not result:
            name = arguments.get("name")
            return f"你没有 {name} 了。" if name else "库存中没有东西。"
        return "\n".join(_item_line(r["name"], r["quantity"]) for r in result)
    if tool_name == "query_batch_details":
        name = arguments["name"]
        if not result:
            return f"你没有 {name} 了。"
        lines = []
        for b in result:
            line = f"{_item_line(name, b['quantity'])}，购买于 {b['purchase_date']}"
            if b["expiry_date"]:
                line += f"，保质期至 {b['expiry_date']}"
            lines.append(line)
        return "\n".join(lines)
    if tool_name == "query_shopping_list":
        if not result:
            return "购物清单是空的。"
        inventory_repo = InventoryRepository(conn)
        names = [inventory_repo.get_item(e.item_id).name for e in result]
        return "购物清单：\n" + "\n".join(names)
    if tool_name == "query_expiring_soon":
        if not result:
            return "没有快过期的东西。"
        return "即将过期：\n" + "\n".join(r["name"] for r in result)
    return "完成。"


def _run_custom_action(conn, household_id: str, user_id: str, description: str) -> str:
    """The Stage-8 fallback: no predefined tool fit, so generate code
    against the safe /internal/sandbox primitives and run it in an
    isolated Modal Sandbox — never in our own process, never with DB
    credentials. Logged to ActionLog like every other action, and the
    generated code is included so it's inspectable after the fact."""
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


def _execute_calls(conn, household_id: str, user_id: str, calls: list) -> str:
    """Runs a list of ToolCalls (from text parsing or receipt parsing)
    and commits once, atomically, at the end. Shared by _process_utterance
    and the receipt-photo path."""
    lines = []
    for call in calls:
        if call.tool_name == "custom_action":
            lines.append(
                _run_custom_action(conn, household_id, user_id, call.arguments["description"])
            )
            continue
        arguments = _coerce_arguments(call.tool_name, call.arguments)
        tool_fn = TOOL_FUNCTIONS[call.tool_name]
        if call.tool_name in ACTION_TOOLS:
            result = tool_fn(conn, household_id, user_id, **arguments)
        else:
            result = tool_fn(conn, household_id, **arguments)
        lines.append(_format_response(conn, call.tool_name, arguments, result))
    conn.commit()

    try:
        # Best-effort: a Telegram hiccup shouldn't fail the user's request.
        run_reminder_check(conn, household_id)
        conn.commit()
    except Exception:
        conn.rollback()

    return "\n".join(lines)


def _process_utterance(conn, household_id: str, user_id: str, text: str) -> str:
    """Shared pipeline for a typed Telegram message: resolve a pending
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
    response_text = _execute_calls(conn, household_id, user_id, parsed.calls)
    _notify_telegram_confirmation(conn, household_id, response_text)
    return response_text


@app.post("/utterance")
def post_utterance(
    payload: UtteranceRequest, user: AuthenticatedUser = Depends(verify_supabase_token)
):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    conn = get_connection()
    try:
        household_id = _get_household_id(conn, user.id)
        response_text = _process_utterance(conn, household_id, user.id, text)
        return {"response": response_text}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/telegram-webhook")
def telegram_webhook(update: dict, request: Request):
    """The primary front end: type a message, or send a receipt photo,
    directly in the household's Telegram group — routed by chat id,
    not a bearer token. Registered with Telegram via
    scripts/set_telegram_webhook.py."""
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and request.headers.get(
        "x-telegram-bot-api-secret-token"
    ) != expected_secret:
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    message = update.get("message") or update.get("edited_message")
    chat_id = message["chat"]["id"] if message else None
    if message is None or chat_id is None:
        return {"ok": True}  # nothing actionable; ack and ignore

    conn = get_connection()
    try:
        household = HouseholdRepository(conn).get_by_telegram_chat_id(chat_id)
        if household is None or not household.member_ids:
            return {"ok": True}  # unrecognized chat; ack and ignore
        user_id = household.member_ids[0]

        photos = message.get("photo")
        text = message.get("text")
        if photos:
            image_bytes = download_telegram_file(photos[-1]["file_id"])  # largest is last
            calls = receipt.parse_receipt(image_bytes)
            response_text = (
                _execute_calls(conn, household.id, user_id, calls)
                if calls
                else "没有从图片中识别出任何商品。"
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


# --- Internal sandbox API (Stage 8) -----------------------------------
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
    unit: str = "unit"
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
        expiry_date = date_cls.fromisoformat(payload.expiry_date) if payload.expiry_date else None
        tools.add_item(
            conn, scope.household_id, scope.user_id, payload.name, payload.quantity, payload.unit, expiry_date
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
        return {"items": [inventory_repo.get_item(e.item_id).name for e in entries]}
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
