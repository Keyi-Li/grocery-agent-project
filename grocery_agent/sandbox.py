"""Sandboxed code-execution fallback.

For requests that don't fit any predefined tool (e.g. "clear all my
stock"), a second LLM call writes a small Python function against a
narrow, curated set of primitives (grocery_agent.api's /internal/sandbox
endpoints — never raw SQL, never the shell), which then runs inside an
isolated Modal Sandbox, not our own process. The sandbox never holds
DB credentials or any other secret — only a short-lived, single-use
callback token scoped to one household's primitives, used to call back
into our own /internal/sandbox API for anything it needs to do.

This trades hand-writing every possible tool for composing the ones
that already exist (e.g. "clear stock" = list stock + consume_item
each one — no new tool needed), while keeping the actual DB/shell/
filesystem out of reach of generated code.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import time
from dataclasses import dataclass

import modal
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# One-time tokens for the sandbox to call back into our own /internal
# API — never the DB credentials themselves. Short-lived, single
# household/user scope, cleared after use or on expiry.
SANDBOX_CALLBACK_TOKEN_TTL_SECONDS = 300
_sandbox_callback_tokens: dict[str, "SandboxCallbackScope"] = {}

# Defense-in-depth on top of sandbox isolation: reject generated code
# that even attempts any of these, rather than trusting isolation alone.
_FORBIDDEN_PATTERNS = re.compile(
    r"\bimport\b|\b__import__\b|\bopen\s*\(|\bexec\s*\(|\beval\s*\(|\bsubprocess\b|\bos\.\b"
)

# ctx methods that actually change data — used to decide whether
# generated code needs human confirmation before running (see
# code_has_write_effect). Read-only primitives (get_stock,
# get_shopping_list, get_expiring_soon) never need confirmation.
_WRITE_PRIMITIVES = (
    "add_item",
    "consume_item",
    "add_to_shopping_list",
    "remove_from_shopping_list",
    "update_item",
)
_WRITE_CALL_PATTERN = re.compile(
    r"\bctx\.(" + "|".join(_WRITE_PRIMITIVES) + r")\s*\("
)


def code_has_write_effect(code: str) -> bool:
    """Whether generated code calls any ctx primitive that changes data.
    Deterministic (a regex over the fixed, known set of ctx methods —
    generated code can't rename or alias them), rather than asking the
    model to judge its own code's risk, which is exactly the kind of
    unreliable judgment call this project has repeatedly gotten wrong
    when left to the LLM."""
    return bool(_WRITE_CALL_PATTERN.search(code))

_CODEGEN_PROMPT = """You write exactly one Python function to satisfy a grocery-inventory \
request that doesn't fit any predefined tool.

def run(ctx):
    ...
    return "<a short confirmation message, in {language}>"

`ctx` has these methods and ONLY these — no other functions, imports, file access, \
or network calls are available or allowed:
- ctx.get_stock(name=None) -> list of {{"name": str, "quantity": float}} \
(all items in stock, or just `name` if given; empty list if none)
- ctx.get_item_details(name) -> list of {{"quantity", "purchase_date", "expiry_date"}}, \
one entry per separate purchase of `name` on record (empty list if none) — read-only
- ctx.add_item(name, quantity, expiry_date=None, stale_after_days=None) -> records a purchase
- ctx.consume_item(name, quantity) -> deducts from stock (floors at zero, never errors)
- ctx.update_item(name, purchase_date=None, expiry_date=None, stale_after_days=None) -> \
corrects a field already recorded for the most recent purchase of `name` (returns True if \
something was found to correct, False otherwise) — a real, permanent change; only use for a \
genuine correction, never a hypothetical
- ctx.add_to_shopping_list(name) -> adds an item to the shopping list
- ctx.remove_from_shopping_list(name) -> removes an item from the shopping list, \
returns True if something was actually removed, False if it wasn't on the list
- ctx.get_shopping_list() -> list of item names on the shopping list
- ctx.get_expiring_soon() -> list of {{"name", "quantity", "expiry_date"}}

Item names in this system are stored in canonical {language}, regardless of \
what language the request used. Any `name` argument you pass to ctx methods \
MUST be that same canonical {language} word, or it will silently fail to \
match existing stock — if the request names an item in a different language \
than {language}, translate it to {language} before calling ctx, never leave \
it in the request's original language.

These modules are already available as plain names — do not write your own \
`import` (none is allowed) — for any computation over data ctx already gave you: \
`date`, `timedelta` (dates from ctx methods are real `date` objects, so e.g. \
`some_date + timedelta(days=3)` works directly), `math`, `statistics`, \
`itertools`, `collections`.

Base the returned message on what the code actually did (check return values \
like remove_from_shopping_list's, or re-query afterward) — never claim an \
action succeeded that ctx has no way to perform or that had no effect.

Return ONLY the function definition — no explanation, no markdown fences, no other text."""


@dataclass
class SandboxCallbackScope:
    household_id: str
    user_id: str
    expires_at: float


def issue_sandbox_callback_token(household_id: str, user_id: str) -> str:
    callback_token = secrets.token_urlsafe(24)
    _sandbox_callback_tokens[callback_token] = SandboxCallbackScope(
        household_id=household_id,
        user_id=user_id,
        expires_at=time.time() + SANDBOX_CALLBACK_TOKEN_TTL_SECONDS,
    )
    return callback_token


def resolve_sandbox_callback_token(callback_token: str) -> SandboxCallbackScope | None:
    scope = _sandbox_callback_tokens.get(callback_token)
    if scope is None or scope.expires_at < time.time():
        _sandbox_callback_tokens.pop(callback_token, None)
        return None
    return scope


def revoke_sandbox_callback_token(callback_token: str) -> None:
    _sandbox_callback_tokens.pop(callback_token, None)


def _llm_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
    )


def generate_action_code(description: str, language: str, client: OpenAI | None = None) -> str:
    """`language` is the requesting household's Household.language."""
    client = client or _llm_client()
    model = os.environ["LLM_MODEL"]
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _CODEGEN_PROMPT.format(language=language)},
            {"role": "user", "content": description},
        ],
    )
    code = (response.choices[0].message.content or "").strip()
    # Strip markdown fences if the model added them despite instructions.
    code = re.sub(r"^```(?:python)?\n?|\n?```$", "", code.strip())
    return code.strip()


def validate_generated_code(code: str) -> None:
    if "def run(ctx)" not in code:
        raise ValueError("generated code doesn't define run(ctx)")
    if _FORBIDDEN_PATTERNS.search(code):
        raise ValueError("generated code uses a disallowed construct")


_RUNNER_TEMPLATE = string.Template(
    r"""
import json
import urllib.request
# Pre-injected for generated code to use directly (no `import` allowed in
# generated code itself — see sandbox._FORBIDDEN_PATTERNS) — all pure
# computation, no I/O capability, so safe to expose unconditionally.
import math
import statistics
import itertools
import collections
from datetime import date, timedelta

BASE_URL = "$base_url"
CALLBACK_TOKEN = "$callback_token"


def _call(endpoint, payload):
    req = urllib.request.Request(
        BASE_URL + "/internal/sandbox/" + endpoint,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + CALLBACK_TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _parse_dates(item, fields):
    for field in fields:
        if item.get(field):
            item[field] = date.fromisoformat(item[field])
    return item


class Context:
    def get_stock(self, name=None):
        return _call("get_stock", {"name": name})["items"]

    def get_item_details(self, name):
        items = _call("get_item_details", {"name": name})["items"]
        return [_parse_dates(item, ("purchase_date", "expiry_date")) for item in items]

    def add_item(self, name, quantity, expiry_date=None, stale_after_days=None):
        return _call(
            "add_item",
            {
                "name": name,
                "quantity": quantity,
                "expiry_date": expiry_date,
                "stale_after_days": stale_after_days,
            },
        )

    def consume_item(self, name, quantity):
        return _call("consume_item", {"name": name, "quantity": quantity})

    def update_item(self, name, purchase_date=None, expiry_date=None, stale_after_days=None):
        return _call(
            "update_item",
            {
                "name": name,
                "purchase_date": purchase_date,
                "expiry_date": expiry_date,
                "stale_after_days": stale_after_days,
            },
        )["updated"]

    def add_to_shopping_list(self, name):
        return _call("add_to_shopping_list", {"name": name})

    def remove_from_shopping_list(self, name):
        return _call("remove_from_shopping_list", {"name": name})["removed"]

    def get_shopping_list(self):
        return _call("get_shopping_list", {})["items"]

    def get_expiring_soon(self):
        items = _call("get_expiring_soon", {})["items"]
        return [_parse_dates(item, ("expiry_date",)) for item in items]


$generated_code

print(run(Context()))
"""
)

_app = modal.App.lookup("grocery-agent-sandbox", create_if_missing=True)
_image = modal.Image.debian_slim()


def run_in_sandbox(code: str, base_url: str, callback_token: str, timeout: int = 60) -> str:
    script = _RUNNER_TEMPLATE.substitute(
        base_url=base_url, callback_token=callback_token, generated_code=code
    )
    sb = modal.Sandbox.create(app=_app, image=_image, timeout=timeout)
    try:
        process = sb.exec("python", "-c", script, timeout=timeout)
        process.wait()
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        if process.returncode != 0:
            raise RuntimeError(f"sandbox exited {process.returncode}: {stderr.strip()}")
        return stdout.strip()
    finally:
        sb.terminate()
