"""Stage 9 — receipt photo parsing.

A photo sent to the household's Telegram group is downloaded, sent to
a vision-capable model via OpenRouter, and parsed into add_item tool
calls — reusing the same canonicalization/language rule as the main
text parser (grocery_agent.llm), since this is a separate LLM call
with its own prompt and doesn't inherit that instruction automatically
(see the Stage 8 note in docs/grocery-agent-plan.md about the same
gap in the sandbox codegen prompt).
"""

from __future__ import annotations

import base64
import json
import os
import re

from dotenv import load_dotenv
from openai import OpenAI

from grocery_agent.dataclass import ALLOWED_UNITS
from grocery_agent.llm import ToolCall

load_dotenv()

DEFAULT_VISION_MODEL = "google/gemini-2.5-flash"

_PROMPT = (
    "Read this grocery receipt image and extract every purchased line item as a "
    "JSON array. Each element: {{\"name\": str, \"quantity\": number, \"unit\": str}}.\n\n"
    "- unit must be one of: {units}\n"
    "- If the receipt shows a quantity or weight directly, use it.\n"
    "- If only a total price and a per-unit price are shown, derive "
    "quantity = total_price / unit_price.\n"
    "- If no quantity is discernible at all, use 1 and unit \"unit\".\n"
    "- Normalize each name to one canonical {language} word, regardless of what "
    "language the receipt is printed in (e.g. \"milk\" and \"牛奶\" both become the "
    "same {language} word) — this must match how items are already named "
    "elsewhere in this system, so always translate to {language}, never leave "
    "the receipt's original language as-is.\n"
    "- Skip non-item lines: tax, subtotal, total, discounts, store name, "
    "payment info.\n\n"
    "Return ONLY the JSON array — no explanation, no markdown fences."
)


def _language() -> str:
    return os.environ.get("RESPONSE_LANGUAGE", "").strip() or "English"


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def parse_receipt(image_bytes: bytes, client: OpenAI | None = None) -> list[ToolCall]:
    client = client or _client()
    model = os.environ.get("VISION_MODEL", DEFAULT_VISION_MODEL)
    b64 = base64.b64encode(image_bytes).decode()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": _PROMPT.format(
                    units=", ".join(sorted(ALLOWED_UNITS)), language=_language()
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the line items from this receipt."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
    )

    text = (response.choices[0].message.content or "[]").strip()
    text = re.sub(r"^```(?:json)?\n?|\n?```$", "", text).strip()
    items = json.loads(text)

    return [
        ToolCall(
            tool_name="add_item",
            arguments={
                "name": item["name"],
                "quantity": item["quantity"],
                "unit": item.get("unit", "unit"),
            },
        )
        for item in items
    ]
