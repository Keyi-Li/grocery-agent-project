"""Stage 1 domain models.

Plain Python dataclasses with validation. No framework, no database,
no I/O — see docs/grocery-agent-plan.md, Section 3 and Stage 1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

# Allowed values for Batch.unit. Not specified explicitly in the plan;
# resolved with the user as a common grocery-unit set, defaulting to
# "unit" when the caller doesn't specify one.
ALLOWED_UNITS = {"unit", "lb", "oz", "kg", "g", "l", "ml", "pack"}

# Allowed values for ShoppingListEntry.source (plan Section 3).
ALLOWED_SHOPPING_LIST_SOURCES = {"auto", "manual"}


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class User:
    email: str
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if not self.email or "@" not in self.email:
            raise ValueError(f"invalid email: {self.email!r}")


@dataclass
class Household:
    name: str
    id: str = field(default_factory=_new_id)
    member_ids: list[str] = field(default_factory=list)
    telegram_chat_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("household name cannot be empty")


@dataclass
class Item:
    name: str
    id: str = field(default_factory=_new_id)
    stale_after_days: int = 5

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("item name cannot be empty")
        if self.stale_after_days < -1:
            raise ValueError(
                "stale_after_days must be -1 (never), 0 (never), or a "
                f"positive integer; got {self.stale_after_days}"
            )


@dataclass
class Batch:
    household_id: str
    item_id: str
    quantity: float
    purchase_date: date
    id: str = field(default_factory=_new_id)
    unit: str = "unit"
    expiry_date: date | None = None

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(f"quantity cannot be negative: {self.quantity}")
        if self.unit not in ALLOWED_UNITS:
            raise ValueError(
                f"unit must be one of {sorted(ALLOWED_UNITS)}; got {self.unit!r}"
            )


@dataclass
class ShoppingListEntry:
    household_id: str
    item_id: str
    source: str
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.source not in ALLOWED_SHOPPING_LIST_SOURCES:
            raise ValueError(
                "source must be one of "
                f"{sorted(ALLOWED_SHOPPING_LIST_SOURCES)}; got {self.source!r}"
            )
