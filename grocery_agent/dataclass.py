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

# Allowed values for ShoppingListEntry.source.
ALLOWED_SHOPPING_LIST_SOURCES = {"auto", "manual"}


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Household:
    name: str
    id: str = field(default_factory=_new_id)
    telegram_chat_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("household name cannot be empty")


@dataclass
class Product:
    """The shared canonical catalog — one row per concept (e.g. "苹果"),
    reused across every household, purely a naming lookup so "milk"
    and "牛奶" resolve to the same row."""

    name: str
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("product name cannot be empty")


@dataclass
class Item:
    """The real, concrete stock: one row per purchase, per household.
    `stale_after_days` lives here rather than on `Product` because
    it's a per-household preference, not a shared catalog fact — two
    households tracking the same product must be able to set their
    own staleness threshold independently."""

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
