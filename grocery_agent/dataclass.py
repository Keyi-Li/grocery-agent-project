"""Domain models.

Plain Python dataclasses with validation. No framework, no database,
no I/O.

There is no `User` class: identity comes directly from Telegram (the
sender's id/name on each message), since Telegram is the only front
end. A household's membership is whoever is in that Telegram group —
Telegram already tracks that, so there's no parallel membership list
to keep in sync.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allowed values for ShoppingListEntry.source.
ALLOWED_SHOPPING_LIST_SOURCES = {"auto", "manual"}


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Household:
    name: str
    id: str = field(default_factory=_new_id)
    telegram_chat_id: str | None = None
    # IANA name (e.g. "America/New_York") — what "6pm" means for this
    # household's daily reminder digest. Households can be in different
    # timezones, so this is per-household data, not a global setting.
    timezone: str = "America/New_York"
    # What language item names get canonicalized to and replies get
    # written in. Free text (whatever an LLM understands as a language
    # name), not validated against a fixed list — same reasoning as
    # timezone: households can differ, so this isn't a global setting.
    language: str = "English"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("household name cannot be empty")
        if not self.language.strip():
            raise ValueError("household language cannot be empty")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError(f"not a real IANA timezone name: {self.timezone!r}") from None


@dataclass
class Product:
    """The shared canonical catalog — one row per concept (e.g. milk),
    reused across every household, purely a naming lookup so the same
    item named differently (a synonym, or a different input language)
    resolves to the same row."""

    name: str
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("product name cannot be empty")


@dataclass
class Item:
    """The real, concrete stock: one row per purchase, per household."""

    household_id: str
    product_id: str
    quantity: float
    purchase_date: date
    id: str = field(default_factory=_new_id)
    expiry_date: date | None = None
    stale_after_days: int = 5

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(f"quantity cannot be negative: {self.quantity}")
        if self.stale_after_days < 0:
            raise ValueError(
                f"stale_after_days must be >= 0 (0 means never); got {self.stale_after_days}"
            )


@dataclass
class ShoppingListEntry:
    household_id: str
    product_id: str
    source: str
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if self.source not in ALLOWED_SHOPPING_LIST_SOURCES:
            raise ValueError(
                "source must be one of "
                f"{sorted(ALLOWED_SHOPPING_LIST_SOURCES)}; got {self.source!r}"
            )


@dataclass
class Recipe:
    """One row of the recipe RAG corpus (see scripts/ingest_recipes.py) —
    shared across every household, not per-household data. `embedding`
    is deliberately not a field here: it's a storage-layer concern
    (RecipeRepository), not part of the domain model any calling code
    should need to see or pass around."""

    name: str
    ingredients: str
    steps: str
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("recipe name cannot be empty")
