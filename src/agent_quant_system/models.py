from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class TradeAction(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class AgentSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    action: TradeAction
    confidence: Decimal = Field(ge=0, le=1)
    target_weight: Decimal = Field(ge=0, le=1)
    thesis: str = Field(min_length=1, max_length=4_000)
    sources: tuple[str, ...] = ()
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not _TICKER_PATTERN.fullmatch(ticker):
            raise ValueError("ticker must be a valid US-market symbol")
        return ticker

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    quantity: Decimal
    market_price: Decimal = Field(gt=0)
    average_cost: Decimal | None = Field(default=None, ge=0)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return AgentSignal.normalize_ticker(value)

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.market_price


class PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    cash: Decimal = Field(ge=0)
    equity: Decimal = Field(gt=0)
    positions: Mapping[str, Position] = Field(default_factory=dict, validate_default=True)
    daily_pnl_fraction: Decimal = Decimal("0")

    @field_validator("positions")
    @classmethod
    def normalize_position_keys(cls, value: Mapping[str, Position]) -> Mapping[str, Position]:
        normalized: dict[str, Position] = {}
        for key, position in value.items():
            ticker = AgentSignal.normalize_ticker(key)
            if ticker != position.ticker:
                raise ValueError("position key must match position ticker")
            if ticker in normalized:
                raise ValueError("duplicate normalized position ticker")
            normalized[ticker] = position
        return MappingProxyType(normalized)

    @field_serializer("positions")
    def serialize_positions(self, value: Mapping[str, Position]) -> dict[str, Position]:
        return dict(value)

    @model_validator(mode="after")
    def reconcile_equity(self) -> Self:
        marked = self.cash + sum(position.market_value for position in self.positions.values())
        if abs(marked - self.equity) > Decimal("0.01"):
            raise ValueError("equity must reconcile with cash and marked positions")
        return self

    @property
    def gross_exposure(self) -> Decimal:
        return sum(
            (abs(position.market_value) for position in self.positions.values()),
            Decimal("0"),
        )

    def position_for(self, ticker: str) -> Position | None:
        return self.positions.get(ticker.upper())


class RiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_confidence: Decimal = Field(default=Decimal("0.65"), ge=0, le=1)
    max_position_weight: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    max_order_notional: Decimal = Field(default=Decimal("5000"), gt=0)
    max_gross_exposure: Decimal = Field(default=Decimal("1.00"), gt=0)
    max_daily_loss_fraction: Decimal = Field(default=Decimal("0.02"), gt=0, le=1)
    allow_short: bool = False
    allow_fractional_shares: bool = True
    max_signal_age_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def position_limit_must_fit_gross_limit(self) -> Self:
        if self.max_position_weight > self.max_gross_exposure:
            raise ValueError("max_position_weight cannot exceed max_gross_exposure")
        return self


class OrderProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str = Field(default_factory=lambda: uuid4().hex)
    ticker: str
    action: TradeAction
    quantity: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    rationale: str
    role: Literal["daily", "entry", "stop", "exit"] = "daily"
    order_type: Literal["LMT", "STP", "MKT"] = "LMT"

    @model_validator(mode="after")
    def require_executable_action(self) -> Self:
        if self.action is TradeAction.HOLD:
            raise ValueError("an order cannot use HOLD")
        expected = {"daily": "LMT", "entry": "LMT", "stop": "STP", "exit": "MKT"}
        if self.order_type != expected[self.role]:
            raise ValueError("order type does not match its execution role")
        if self.role in ("stop", "exit") and self.action is not TradeAction.SELL:
            raise ValueError("protection and exit orders must reduce long positions")
        if self.role == "entry" and self.action is not TradeAction.BUY:
            raise ValueError("intraday entries must be long-only")
        return self

    @property
    def estimated_notional(self) -> Decimal:
        return self.quantity * self.reference_price


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    reasons: tuple[str, ...] = ()
    order: OrderProposal | None = None

    @model_validator(mode="after")
    def check_consistency(self) -> Self:
        if self.approved and self.order is None:
            raise ValueError("approved decisions require an order")
        if not self.approved and self.order is not None:
            raise ValueError("rejected decisions cannot contain an order")
        return self


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str
    broker_order_id: str
    status: str
    ticker: str
    action: TradeAction
    filled_quantity: Decimal = Field(ge=0)
    average_price: Decimal = Field(ge=0)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
