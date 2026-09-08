"""Sandboxed code-execution fallback.

For requests that don't fit any predefined tool (e.g. "clear all my
stock"), a second LLM call writes a small Python function against a
narrow, curated set of primitives (grocery_agent.api's /internal/sandbox
endpoints — never raw SQL, never the shell), which then runs inside an
isolated Modal Sandbox, not our own process. The sandbox never holds
DB credentials or any other secret — only a short-lived, single-use
token scoped to one household's primitives.

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
SANDBOX_TOKEN_TTL_SECONDS = 300
_sandbox_tokens: dict[str, "SandboxTokenScope"] = {}

# Defense-in-depth on top of sandbox isolation: reject generated code
# that even attempts any of these, rather than trusting isolation alone.
_FORBIDDEN_PATTERNS = re.compile(
    r"\bimport\b|\b__import__\b|\bopen\s*\(|\bexec\s*\(|\beval\s*\(|\bsubprocess\b|\bos\.\b"
)

_CODEGEN_PROMPT = """You write exactly one Python function to satisfy a grocery-inventory \
request that doesn't fit any predefined tool.

def run(ctx):
    ...
    return "<a short confirmation message, in {language}>"

`ctx` has these methods and ONLY these — no other functions, imports, file access, \
or network calls are available or allowed:
- ctx.get_stock(name=None) -> list of {{"name": str, "quantity": float}} \
(all items in stock, or just `name` if given; empty list if none)
- ctx.add_item(name, quantity, expiry_date=None) -> records a purchase
- ctx.consume_item(name, quantity) -> deducts from stock (floors at zero, never errors)
- ctx.add_to_shopping_list(name) -> adds an item to the shopping list
- ctx.get_shopping_list() -> list of item names on the shopping list
- ctx.get_expiring_soon() -> list of {{"name", "quantity", "expiry_date"}}

Item names in this system are stored in canonical {language} (e.g. "milk" is \
stored as its {language} word), regardless of what language the request used. \
Any `name` argument you pass to ctx methods MUST be that same canonical \
{language} word, or it will silently fail to match existing stock — e.g. if \
the request mentions "pear" but the system's language is Chinese, use "梨", \
not "pear".

Return ONLY the function definition — no explanation, no markdown fences, no other text."""


def _codegen_language() -> str:
    return os.environ.get("RESPONSE_LANGUAGE", "").strip() or "English"


@dataclass
class SandboxTokenScope:
    household_id: str
    user_id: str
    expires_at: float


def issue_sandbox_token(household_id: str, user_id: str) -> str:
    token = secrets.token_urlsafe(24)
    _sandbox_tokens[token] = SandboxTokenScope(
        household_id=household_id,
        user_id=user_id,
        expires_at=time.time() + SANDBOX_TOKEN_TTL_SECONDS,
    )
    return token


def resolve_sandbox_token(token: str) -> SandboxTokenScope | None:
    scope = _sandbox_tokens.get(token)
    if scope is None or scope.expires_at < time.time():
        _sandbox_tokens.pop(token, None)
        return None
    return scope


def revoke_sandbox_token(token: str) -> None:
    _sandbox_tokens.pop(token, None)


def _openrouter_client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def generate_code(description: str, client: OpenAI | None = None) -> str:
    client = client or _openrouter_client()
    model = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _CODEGEN_PROMPT.format(language=_codegen_language())},
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

BASE_URL = "$base_url"
TOKEN = "$token"


def _call(endpoint, payload):
    req = urllib.request.Request(
        BASE_URL + "/internal/sandbox/" + endpoint,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


class Context:
    def get_stock(self, name=None):
        return _call("get_stock", {"name": name})["items"]

    def add_item(self, name, quantity, expiry_date=None):
        return _call(
            "add_item",
            {"name": name, "quantity": quantity, "expiry_date": expiry_date},
        )

    def consume_item(self, name, quantity):
        return _call("consume_item", {"name": name, "quantity": quantity})

    def add_to_shopping_list(self, name):
        return _call("add_to_shopping_list", {"name": name})

    def get_shopping_list(self):
        return _call("get_shopping_list", {})["items"]

    def get_expiring_soon(self):
        return _call("get_expiring_soon", {})["items"]


$generated_code

print(run(Context()))
"""
)

_app = modal.App.lookup("grocery-agent-sandbox", create_if_missing=True)
_image = modal.Image.debian_slim()


def run_in_sandbox(code: str, base_url: str, token: str, timeout: int = 60) -> str:
    script = _RUNNER_TEMPLATE.substitute(base_url=base_url, token=token, generated_code=code)
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
