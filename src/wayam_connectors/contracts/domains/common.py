from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217")]


class Money(BaseModel):
    """Decimal amount with explicit currency. Never binary floating point."""

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: CurrencyCode

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, v: object) -> object:
        if isinstance(v, float):
            raise ValueError("money amount must be Decimal/str/int, not float")
        return v

    @property
    def minor_units(self) -> int:
        """Integer minor units assuming 2 decimals unless the currency is 0/3-decimal (documented subset)."""
        exp = {"JPY": 0, "KRW": 0, "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3}.get(self.currency, 2)
        q = Decimal(1).scaleb(-exp)
        return int((self.amount.quantize(q)).scaleb(exp))


class DomainRecord(BaseModel):
    """Every domain record carries immutable provider identity and freshness."""

    model_config = ConfigDict(frozen=True)

    source_system: str
    source_object_id: str
    source_version: str | None = None
    observed_at_iso: str = Field(description="ISO-8601 UTC timestamp of observation")
