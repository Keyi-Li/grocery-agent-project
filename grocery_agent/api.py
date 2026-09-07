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

from grocery_agent import tools
from grocery_agent.db import get_connection
from grocery_agent.llm import parse_utterance
from grocery_agent.notifications import send_telegram_message
from grocery_agent.reminders import run_reminder_check
from grocery_agent.repositories import HouseholdRepository, InventoryRepository

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

    1. A static SIRI_SHORTCUT_TOKEN, mapped directly to
       SIRI_DEFAULT_USER_ID (set by scripts/setup_household.py). This
       is what the Siri Shortcut uses — a plain Shortcut can't run a
       Supabase login/refresh-token flow on its own, so a fixed
       never-expiring secret is the practical choice for that client.
    2. Otherwise, a real Supabase Auth access token, verified by
       asking Supabase's own Auth server whose token it is. This path
       is what any future real client (a proper mobile app, a web UI)
       would use instead.

    Overridden in tests via FastAPI dependency_overrides so tests
    don't need a live Supabase Auth session."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ")

    siri_token = os.environ.get("SIRI_SHORTCUT_TOKEN")
    siri_user_id = os.environ.get("SIRI_DEFAULT_USER_ID")
    if siri_token and siri_user_id and token == siri_token:
        return AuthenticatedUser(id=siri_user_id, email="")

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
    if tool_name == "add_item" and arguments.get("expiry_date"):
        arguments["expiry_date"] = date_cls.fromisoformat(arguments["expiry_date"])
    return arguments


_PLURAL_UNITS = {"unit": "units", "pack": "packs"}


def _format_quantity(quantity: float) -> str:
    return str(int(quantity)) if quantity == int(quantity) else str(quantity)


def _pluralize_unit(unit: str, quantity: float) -> str:
    return unit if quantity == 1 else _PLURAL_UNITS.get(unit, unit)


def _pluralize_name(name: str, quantity: float) -> str:
    return name if quantity == 1 or name.endswith("s") else f"{name}s"


def _format_response(conn, tool_name: str, arguments: dict, result) -> str:
    if tool_name == "add_item":
        quantity = arguments["quantity"]
        unit = arguments.get("unit", "unit")
        name = arguments["name"]
        if unit == "unit":
            return f"Added {_format_quantity(quantity)} {_pluralize_name(name, quantity)}."
        return (
            f"Added {_format_quantity(quantity)} {_pluralize_unit(unit, quantity)} "
            f"of {name}."
        )
    if tool_name == "consume_item":
        quantity = arguments["quantity"]
        return (
            f"Recorded {_format_quantity(quantity)} "
            f"{_pluralize_name(arguments['name'], quantity)} consumed."
        )
    if tool_name == "add_to_shopping_list":
        return f"Added {arguments['name']} to the shopping list."
    if tool_name == "query_stock":
        if not result:
            name = arguments.get("name")
            return f"You have no {name}." if name else "You have nothing in stock."
        parts = []
        for r in result:
            q = _format_quantity(r["quantity"])
            if r["unit"] == "unit":
                parts.append(f"{q} {_pluralize_name(r['name'], r['quantity'])}")
            else:
                parts.append(f"{q} {_pluralize_unit(r['unit'], r['quantity'])} of {r['name']}")
        return "; ".join(parts)
    if tool_name == "query_shopping_list":
        if not result:
            return "Your shopping list is empty."
        inventory_repo = InventoryRepository(conn)
        names = [inventory_repo.get_item(e.item_id).name for e in result]
        return "Shopping list: " + ", ".join(names)
    if tool_name == "query_expiring_soon":
        if not result:
            return "Nothing is expiring soon."
        return "Expiring soon: " + ", ".join(r["name"] for r in result)
    return "Done."


def _process_utterance(conn, household_id: str, user_id: str, text: str) -> str:
    """Shared pipeline for any client (Siri Shortcut, Telegram): resolve
    a pending clarification if one's outstanding, parse, dispatch the
    tool, run the opportunistic reminder check, and notify Telegram.
    Returns the response text."""
    pending = _pending_clarifications.get(user_id)
    is_fresh_pending = (
        pending is not None
        and time.time() - pending.asked_at < PENDING_CLARIFICATION_TTL_SECONDS
    )
    if is_fresh_pending:
        combined = (
            f'{pending.original_text}\n'
            f'(You asked: "{pending.question}" — the user answered: "{text}")'
        )
        parsed = parse_utterance(combined)
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

    arguments = _coerce_arguments(parsed.tool_name, parsed.arguments)
    tool_fn = TOOL_FUNCTIONS[parsed.tool_name]
    if parsed.tool_name in ACTION_TOOLS:
        result = tool_fn(conn, household_id, user_id, **arguments)
    else:
        result = tool_fn(conn, household_id, **arguments)
    conn.commit()

    try:
        # Best-effort: a Telegram hiccup shouldn't fail the user's request.
        run_reminder_check(conn, household_id)
        conn.commit()
    except Exception:
        conn.rollback()

    response_text = _format_response(conn, parsed.tool_name, arguments, result)
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
    """Lets you type directly in the household's Telegram group instead
    of using Siri — same parsing/tool pipeline, routed by chat id
    instead of a bearer token. Registered with Telegram via
    scripts/set_telegram_webhook.py."""
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and request.headers.get(
        "x-telegram-bot-api-secret-token"
    ) != expected_secret:
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    message = update.get("message") or update.get("edited_message")
    text = message.get("text") if message else None
    chat_id = message["chat"]["id"] if message else None
    if not text or chat_id is None:
        return {"ok": True}  # nothing actionable (e.g. a sticker); ack and ignore

    conn = get_connection()
    try:
        household = HouseholdRepository(conn).get_by_telegram_chat_id(chat_id)
        if household is None or not household.member_ids:
            return {"ok": True}  # unrecognized chat; ack and ignore
        _process_utterance(conn, household.id, household.member_ids[0], text.strip())
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
