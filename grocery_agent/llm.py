"""Stage 5 — LLM parsing layer.

Turns a raw voice utterance into a selected Stage-4 tool call (name +
arguments) or a clarifying question, using OpenRouter's OpenAI-
compatible tool-calling API. See docs/grocery-agent-plan.md Section 4
(tool definitions, ambiguity handling) and Section 3a (multilingual
normalization).

Uses OpenRouter rather than calling Anthropic directly — a deliberate
choice for this project (existing OpenRouter credit; this parsing task
is simple enough that a cheap model is plenty).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import OpenAI

from grocery_agent.dataclass import ALLOWED_UNITS

load_dotenv()

DEFAULT_MODEL = "qwen/qwen3.7-flash"

SYSTEM_PROMPT = """You are the natural-language front end for a household \
grocery inventory tracker.

For every utterance, pick exactly one of the available tools and fill \
in its arguments. If you cannot confidently fill a required argument \
(for example, no quantity was stated for a consume/add action), do \
NOT call any tool — instead respond with a short, direct clarifying \
question in plain text, and nothing else.

Always normalize any item name (the `name` argument) to a single \
canonical form: English, lowercase, singular — regardless of what \
language or phrasing the user used (e.g. both "milk" and "牛奶" must \
become "milk"). This applies no matter what language the utterance is \
in.

Quantities may be written as digits ("1", "3") or spelled out as \
words, in any language — including Chinese numerals like "一" (one), \
"两"/"二" (two), "三" (three). Always resolve these to a plain number \
for the `quantity` argument; a spelled-out number is not a reason to \
ask for clarification."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_item",
            "description": "Record a new purchase of an item, adding to stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Canonical item name.",
                    },
                    "quantity": {"type": "number"},
                    "unit": {"type": "string", "enum": sorted(ALLOWED_UNITS)},
                    "expiry_date": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD), only if mentioned.",
                    },
                },
                "required": ["name", "quantity", "unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consume_item",
            "description": "Record consumption of an item, deducting from stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "number"},
                },
                "required": ["name", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_shopping_list",
            "description": "Manually add an item to the shopping list.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_stock",
            "description": "Look up current stock, for one item or the whole inventory.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_shopping_list",
            "description": "List everything currently on the shopping list.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_expiring_soon",
            "description": "List items expiring within the next couple of days.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


@dataclass
class ParsedCommand:
    tool_name: str | None
    arguments: dict = field(default_factory=dict)
    clarification: str | None = None

    @property
    def needs_clarification(self) -> bool:
        return self.tool_name is None


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def parse_utterance(text: str, client: OpenAI | None = None) -> ParsedCommand:
    client = client or _client()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        tools=TOOLS,
        tool_choice="auto",
    )
    message = response.choices[0].message

    if message.tool_calls:
        call = message.tool_calls[0]
        arguments = json.loads(call.function.arguments) if call.function.arguments else {}
        return ParsedCommand(tool_name=call.function.name, arguments=arguments)

    clarification = (message.content or "").strip()
    return ParsedCommand(
        tool_name=None, arguments={}, clarification=clarification or None
    )
