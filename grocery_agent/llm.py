"""LLM parsing layer.

Turns a raw utterance into a tool call (name + arguments) or a
clarifying question, using OpenRouter's OpenAI-compatible tool-calling
API.

Uses OpenRouter rather than calling a model provider directly — a
deliberate choice for this project (existing OpenRouter credit; this
parsing task is simple enough that a cheap model is plenty).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_MODEL = "qwen/qwen3.7-flash"

# RESPONSE_LANGUAGE drives both: clarifying questions get forced into
# it, and it's also the one canonical language every item name gets
# normalized to (must be a single fixed language, not "mirror input" —
# otherwise the same item said in two languages would create two
# different DB rows). Defaults to English if unset. Not a per-household
# setting yet — a global default, easy to make one later.
_PROMPT = (
    "Today is {today}. Parse grocery utterances into tool calls, filling all "
    "required arguments — one call per item/action (e.g. two items mentioned "
    "means two add_item calls, not one custom_action). If a required argument "
    "is missing, don't call a tool — ask one short clarifying question instead, "
    "in {language} (one sentence, no lists).\n\n"
    "Normalize item names to one canonical {language} word, regardless of what "
    'language the utterance is in (e.g. "milk" and "牛奶" both become the same '
    "{language} word). Quantities may be digits or number words in any language "
    '(Chinese "一"/"两" = 1/2) — always resolve to a number, never ask for '
    "clarification just because a number was spelled out. Resolve partial or "
    "relative dates (e.g. \"August 31\") against today's date, picking the next "
    "upcoming occurrence — this applies to both purchase_date and expiry_date. "
    "If no purchase date is mentioned or discernible (e.g. from a receipt), "
    "omit purchase_date entirely rather than guessing; it defaults to today.\n\n"
    "custom_action is a LAST RESORT ONLY, for requests no combination of the "
    "other tools above can satisfy (e.g. 'clear all my stock', conditional "
    "logic). Never use it for something a normal tool call (or several) "
    "already handles — multiple items, multiple actions, and querying-then-"
    "acting are not reasons to use custom_action by themselves."
)


def _system_prompt() -> str:
    language = os.environ.get("RESPONSE_LANGUAGE", "").strip() or "English"
    return _PROMPT.format(language=language, today=date.today().isoformat())

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
                    "purchase_date": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD), only if mentioned or "
                        "shown on a receipt — omit to default to today.",
                    },
                    "expiry_date": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD), only if mentioned.",
                    },
                },
                "required": ["name", "quantity"],
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
            "name": "query_item_details",
            "description": (
                "Per-purchase detail for one item — purchase date, expiry date, "
                "quantity. Use this for questions query_stock can't answer since "
                "it only gives a total (e.g. 'when did I buy the milk?', 'how old "
                "is the beef?')."
            ),
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
    {
        "type": "function",
        "function": {
            "name": "custom_action",
            "description": (
                "Last resort ONLY, when none of the other tools can accomplish what "
                "the user asked (e.g. 'clear all my stock', bulk operations, anything "
                "not covered above). Describe precisely what should happen — this "
                "triggers a sandboxed code-generation step, not an immediate action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Precise description of the desired outcome.",
                    }
                },
                "required": ["description"],
            },
        },
    },
]


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ParsedCommand:
    """A single utterance can name more than one item (e.g. "一块面包，
    一盒饼干" — a loaf of bread AND a pack of cookies), so this holds a
    list of calls, not just one."""

    calls: list[ToolCall] = field(default_factory=list)
    clarification: str | None = None

    @property
    def needs_clarification(self) -> bool:
        return not self.calls

    @property
    def tool_name(self) -> str | None:
        """Convenience for the common single-call case."""
        return self.calls[0].tool_name if self.calls else None

    @property
    def arguments(self) -> dict:
        """Convenience for the common single-call case."""
        return self.calls[0].arguments if self.calls else {}


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def parse_utterance(
    utterance: str | list[dict], client: OpenAI | None = None
) -> ParsedCommand:
    """`utterance` is either a single string (a fresh utterance) or a
    list of {"role", "content"} turns — used to resolve a pending
    clarification as a real conversation (user asked -> assistant
    asked back -> user answered) rather than one mashed-together
    string."""
    messages = [{"role": "user", "content": utterance}] if isinstance(utterance, str) else utterance
    client = client or _client()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _system_prompt()}, *messages],
        tools=TOOLS,
        tool_choice="auto",
    )
    message = response.choices[0].message

    if message.tool_calls:
        calls = [
            ToolCall(
                tool_name=c.function.name,
                arguments=json.loads(c.function.arguments) if c.function.arguments else {},
            )
            for c in message.tool_calls
        ]
        return ParsedCommand(calls=calls)

    clarification = (message.content or "").strip()
    return ParsedCommand(calls=[], clarification=clarification or None)


_REPLY_PROMPT = (
    "Report these grocery-inventory facts to the household. They relate to: "
    "{context}\n\n"
    "Write ONE short, concise message, in the same language as that request "
    "if it's natural-language text — otherwise (e.g. a receipt photo, or a "
    "proactive reminder with no request behind it) use {fallback_language}. "
    "Item names below are stored in a fixed canonical form — express them "
    "naturally in the reply's language rather than copying that form "
    "verbatim if it doesn't match.\n\n"
    'Style: terse and information-dense, e.g. "苹果 +3" for an addition, '
    '"牛奶 -1" for a consumption, "牛奶 purchased 2026-01-01, expires '
    '2026-01-05" for a detail lookup, "牛奶 快过期了（2026-01-05）" for an '
    'expiry reminder, or "苹果 放了很久没动，还在吗？" for a staleness '
    "reminder. One line per fact if there are several. No pleasantries, no "
    "explanations — just the facts."
)


def generate_reply(
    context: str | None, facts: list[dict], client: OpenAI | None = None
) -> str:
    """Turns the raw facts of what just happened (Python-resolved, since
    only Python has DB access) into the actual reply text — the LLM
    decides wording/language, not a hardcoded template."""
    client = client or _client()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    fallback_language = os.environ.get("RESPONSE_LANGUAGE", "").strip() or "English"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": _REPLY_PROMPT.format(
                    context=context or "(no specific request)", fallback_language=fallback_language
                ),
            },
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)},
        ],
    )
    return (response.choices[0].message.content or "").strip()
