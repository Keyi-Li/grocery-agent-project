"""LLM parsing layer.

Turns a raw utterance into a tool call (name + arguments) or a
clarifying question, via any OpenAI-compatible tool-calling API
(LLM_BASE_URL — OpenRouter, a self-hosted proxy, etc.).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# The household's language (Household.language) drives both: clarifying
# questions get forced into it, and it's also the one canonical language
# every item name gets normalized to (must be a single fixed language,
# not "mirror input" — otherwise the same item said in two languages
# would create two different DB rows).
_PROMPT = (
    "Today is {today}. You parse grocery utterances into tool calls, in {language}.\n\n"

    "Splitting:\n"
    "- One tool call per item/action. Two items or two actions mentioned "
    "means two calls (e.g. two add_item, or one consume_item + one "
    "add_to_shopping_list) — never merge into a single custom_action.\n"
    "- Missing a required argument? Don't call a tool — ask one short "
    "clarifying question (one sentence, no lists).\n\n"

    "Normalization:\n"
    "- Item names: resolve to one canonical {language} word regardless of "
    "input language — a synonym or the same item named in a different "
    "language must resolve to the same {language} word every time.\n"
    "- Quantities: resolve digits or number words in any language to a "
    "number (e.g. a spelled-out count word like \"two\" = 2). Never ask for clarification just "
    "because a number was spelled out.\n\n"

    "Dates:\n"
    "- Resolve partial/relative dates (\"August 31\") against today, picking "
    "the next upcoming occurrence. Applies to purchase_date and "
    "expiry_date alike.\n"
    "- No purchase date discernible? Omit purchase_date — it defaults to "
    "today. Don't guess.\n"
    "- A correction to something already recorded (\"actually bought 3 days "
    "ago\") → update_item. Never add_item again for this; that would "
    "record a spurious second purchase.\n"
    "- A conditional/hypothetical question (\"what if it had been bought 3 "
    "days ago\") changes nothing on record → custom_action, not "
    "update_item.\n"
    "- Unclear which is meant? Ask.\n\n"

    "custom_action mechanics:\n"
    "- Its result is a real tool result you'll see and can act on — but "
    "your own plain-text output is discarded once you stop calling tools. "
    "So for any computation whose answer must reach the user (e.g. a "
    "shifted/hypothetical date), call custom_action to do the math; never "
    "compute it yourself in prose and treat that as the final answer.\n\n"

    "Multi-step requests:\n"
    "- You may see real results from tool calls you already made earlier "
    "in this request — use them to decide the next call, and keep calling "
    "tools until the request is fully handled.\n"
    "- Done? Respond with plain text instead of a tool call to signal "
    "that (this text is discarded, so it doesn't need to be a real "
    "answer)."
)


def _system_prompt(language: str) -> str:
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
                    "stale_after_days": {
                        "type": "integer",
                        "description": "Days of no consumption before flagging as "
                        "possibly-forgotten, only if the user states or implies an "
                        "unusually long or short shelf life (e.g. canned goods, "
                        "frozen food) — omit to use the system default (5 days). "
                        "0 means never flag this item as stale.",
                    },
                },
                "required": ["name", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_item",
            "description": (
                "Correct one or more fields already recorded for an item — a real, "
                "permanent change. Use only when the user's intent is clearly a "
                "correction (e.g. 'actually bought 3 days ago', 'that lasts a "
                "month, not 5 days'), never for a hypothetical/what-if question "
                "(that changes nothing, so it's a custom_action case instead — "
                "use get_item_details there and compute, don't guess). If "
                "genuinely ambiguous which one is meant, ask a short clarifying "
                "question rather than guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "purchase_date": {
                        "type": "string",
                        "description": "Corrected purchase date, ISO date (YYYY-MM-DD).",
                    },
                    "expiry_date": {
                        "type": "string",
                        "description": "Corrected expiry date, ISO date (YYYY-MM-DD).",
                    },
                    "stale_after_days": {
                        "type": "integer",
                        "description": "Corrected staleness threshold in days (0 = never).",
                    },
                },
                "required": ["name"],
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
            "name": "remove_from_shopping_list",
            "description": "Remove one item from the shopping list.",
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
                "quantity, staleness threshold. Use this for questions query_stock "
                "can't answer since it only gives a total (e.g. 'when did I buy "
                "the milk?', 'how old is the beef?', 'after how many days do you "
                "flag my rice as forgotten?')."
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
            "name": "suggest_recipes",
            "description": "Suggest real recipes that use what's currently in stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "preference": {
                        "type": "string",
                        "description": "Any stated constraint, e.g. \"quick\" or "
                        "\"vegetarian\". Omit if none was given.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "custom_action",
            "description": (
                "Last resort ONLY — use when no combination of the other tools "
                "can satisfy the request, even across several calls (e.g. "
                "conditional/hypothetical logic, or bulk operations like "
                "'clear all my stock'). Never use it for something a sequence "
                "of normal calls already handles: multiple items, multiple "
                "actions, or query-then-act (e.g. 'clear the shopping list' = "
                "query it, then remove each item). Describe precisely what "
                "should happen — this triggers a sandboxed code-generation "
                "step, not an immediate action."
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
    # The model's own id for this call, only set when parse_utterance got it
    # from a real tool-calling response — needed to feed a matching "tool"
    # role result back for a multi-turn conversation (see api._run_tool_loop).
    # None for calls built elsewhere (receipt parsing, tests).
    call_id: str | None = None


@dataclass
class ParsedCommand:
    """A single utterance can name more than one item (e.g. "a loaf of
    bread and a pack of cookies" — two distinct items), so this holds a
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
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
    )


def parse_utterance(
    utterance: str | list[dict], language: str, client: OpenAI | None = None
) -> ParsedCommand:
    """`utterance` is either a single string (a fresh utterance) or a
    list of {"role", "content"} turns — used to resolve a pending
    clarification as a real conversation (user asked -> assistant
    asked back -> user answered) rather than one mashed-together
    string. `language` is the requesting household's Household.language."""
    messages = [{"role": "user", "content": utterance}] if isinstance(utterance, str) else utterance
    client = client or _client()
    model = os.environ["LLM_MODEL"]

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _system_prompt(language)}, *messages],
        tools=TOOLS,
        tool_choice="auto",
    )
    message = response.choices[0].message

    if message.tool_calls:
        calls = [
            ToolCall(
                tool_name=c.function.name,
                arguments=json.loads(c.function.arguments) if c.function.arguments else {},
                call_id=c.id,
            )
            for c in message.tool_calls
        ]
        return ParsedCommand(calls=calls)

    clarification = (message.content or "").strip()
    return ParsedCommand(calls=[], clarification=clarification or None)


_REPLY_PROMPT = (
    "Report these grocery-inventory facts to the household. They relate to: "
    "{context}\n\n"
    "Language: match the request's language if it's natural-language text. "
    "Otherwise — a receipt photo, or a proactive reminder with no request "
    "behind it — use {fallback_language}. Item names are stored in a fixed "
    "canonical form; express them naturally in the reply's language rather "
    "than copying that form verbatim if it doesn't match.\n\n"
    "Style: one short, terse, information-dense message. No pleasantries, "
    "no explanations — just the facts, one line per fact if there are "
    'several. Examples: "milk +3" for an addition, "eggs -1" for a '
    'consumption, "milk purchased 2026-01-01, expires 2026-01-05" for a '
    'detail lookup. A confirm_custom_action fact is a genuine yes/no '
    'question — phrase it as one, e.g. "about to run: <description>, '
    'confirm?", never as a completed-action statement.\n\n'
    "A reminder_digest fact has expiring_items and stale_items lists — "
    "produce up to two labeled sections (skip a section entirely if its "
    "list is empty), one line per item within a section as "
    '"<name> <quantity>, <date label> <date>" (expiry_date for '
    "expiring_items, purchase_date for stale_items), e.g.:\n"
    "Expiring soon:\n"
    "milk 1, expires 2026-01-05\n\n"
    "Stale (unused for a while):\n"
    "eggs 10, purchased 2026-01-01\n\n"
    "A query_shopping_list fact has manual_items and auto_items lists — "
    "same two-section treatment (skip a section if empty), item names "
    "only, comma-separated within a section, e.g.:\n"
    "Shopping list:\n"
    "hand soap, rice\n\n"
    "Ran out automatically:\n"
    "milk, eggs\n\n"
    "A suggest_recipes fact has a recipes list (name, ingredients, "
    "steps for each, as retrieved real recipes — never invent or alter "
    "one). List each by name with a one-line paraphrase of how it uses "
    "what's in stock; don't restate the full ingredients or steps "
    "verbatim. If the list is empty, say plainly that nothing matched, "
    "not that stock is empty."
)


def generate_reply(
    context: str | None, facts: list[dict], language: str, client: OpenAI | None = None
) -> str:
    """Turns the raw facts of what just happened (Python-resolved, since
    only Python has DB access) into the actual reply text — the LLM
    decides wording, not a hardcoded template. `language` (the
    requesting household's Household.language) is the fallback used
    when there's no request text to mirror (a receipt photo, a
    reminder)."""
    client = client or _client()
    model = os.environ["LLM_MODEL"]
    fallback_language = language

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


_AFFIRMATIVE_PROMPT = (
    "Does this message affirmatively confirm going ahead with a proposed "
    "action (e.g. yes, OK, go ahead, confirm, in any language)? Reply with "
    "exactly one word: YES or NO."
)


def is_affirmative(text: str, client: OpenAI | None = None) -> bool:
    """Classifies a short reply as confirming a pending action or not — a
    deliberately tiny, separate call rather than folding this into the
    main tool-calling turn, so a yes/no reply is never itself parsed as
    a new, unrelated grocery request."""
    client = client or _client()
    model = os.environ["LLM_MODEL"]
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _AFFIRMATIVE_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    return (response.choices[0].message.content or "").strip().upper().startswith("YES")


_ONBOARDING_PROMPT = (
    "Extract a household name and, if given, a timezone and a display "
    "language from this onboarding message — the sender is answering "
    "\"what should we call your household, and optionally what timezone "
    "and language do you want\", in their own words/order/language. "
    "Resolve a place name to its IANA timezone (e.g. \"Munich\" -> "
    "\"Europe/Berlin\"). A magic word may also be in the message — ignore "
    "it, it's handled separately. Call submit_onboarding only if a "
    "household name is clearly present."
)

_ONBOARDING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_onboarding",
            "description": "Record the extracted onboarding fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "household_name": {"type": "string"},
                    "timezone": {
                        "type": "string",
                        "description": "IANA name, e.g. America/New_York. Omit if not given.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Display language name, e.g. Chinese. Omit if not given.",
                    },
                },
                "required": ["household_name"],
            },
        },
    }
]


def parse_onboarding_reply(text: str, client: OpenAI | None = None) -> dict | None:
    """Extracts household_name/timezone/language from a free-text
    onboarding reply — the magic-word check itself stays a deterministic
    Python string comparison in api.py (only reached once that already
    passed), so this never influences whether a household actually gets
    created, only what it's named/configured as. Returns None if no
    household name was found."""
    client = client or _client()
    model = os.environ["LLM_MODEL"]
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _ONBOARDING_PROMPT},
            {"role": "user", "content": text},
        ],
        tools=_ONBOARDING_TOOLS,
        tool_choice="auto",
    )
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        return None
    return json.loads(tool_calls[0].function.arguments)
