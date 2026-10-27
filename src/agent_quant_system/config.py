from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_quant_system.models import AgentSignal, RiskLimits

_LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY_RISK"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AQS_",
        extra="ignore",
    )

    environment: Literal["development", "paper", "live"] = "paper"
    research_provider: Literal["static", "tradingagents"] = "static"
    execution_provider: Literal["paper", "nautilus"] = "paper"
    live_trading_enabled: bool = False
    live_confirmation: str = ""

    llm_provider: str = "openai"
    quick_think_llm: str = "gpt-5-mini"
    deep_think_llm: str = "gpt-5"
    agent_target_weight: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)

    min_confidence: Decimal = Field(default=Decimal("0.65"), ge=0, le=1)
    max_position_weight: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    max_order_notional: Decimal = Field(default=Decimal("5000"), gt=0)
    max_gross_exposure: Decimal = Field(default=Decimal("1.00"), gt=0)
    max_daily_loss_fraction: Decimal = Field(default=Decimal("0.02"), gt=0, le=1)
    allow_short: bool = False
    allow_fractional_shares: bool = True
    max_signal_age_seconds: int = Field(default=300, ge=1)

    ib_account_id: str = ""
    ib_gateway_host: str = "127.0.0.1"
    ib_gateway_port: int = Field(default=4002, ge=1, le=65535)
    ib_client_id: int = Field(default=71, ge=1)
    ib_timeout_seconds: int = Field(default=15, ge=1, le=60)
    paper_trading_enabled: bool = False
    monitor_symbols: str = "AAPL,MSFT,NVDA,AMZN,GOOGL"
    monitor_interval_seconds: int = Field(default=60, ge=15, le=3600)
    monitor_state_path: Path = Path("data/monitor.sqlite3")
    monitor_stop_path: Path = Path("data/STOP")
    quote_max_age_seconds: int = Field(default=30, ge=1, le=120)
    max_spread_bps: Decimal = Field(default=Decimal("20"), gt=0, le=100)
    order_limit_offset_bps: Decimal = Field(default=Decimal("5"), ge=0, le=20)
    order_cash_buffer: Decimal = Field(default=Decimal("5"), ge=0)
    notification_webhook: SecretStr | None = None
    strategy_mode: Literal["daily", "intraday"] = "daily"
    intraday_symbols: str = "SPY,QQQ"
    intraday_interval_seconds: int = Field(default=5, ge=1, le=15)
    intraday_max_gross: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    intraday_daily_loss: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    intraday_cash_reserve: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    intraday_max_positions: int = Field(default=3, ge=1, le=5)
    intraday_max_entries: int = Field(default=10, ge=1, le=100)
    intraday_stop_fraction: Decimal = Field(default=Decimal("0.005"), gt=0, lt=1)
    intraday_entry_timeout: int = Field(default=60, ge=10, le=300)
    intraday_ack_timeout: int = Field(default=180, ge=30, le=600)

    def watchlist(self) -> tuple[str, ...]:
        symbols = (
            self.intraday_symbols if self.strategy_mode == "intraday" else self.monitor_symbols
        )
        tickers = tuple(AgentSignal.normalize_ticker(item) for item in symbols.split(","))
        if not 1 <= len(tickers) <= 20 or len(set(tickers)) != len(tickers):
            raise ValueError("monitor requires 1-20 unique symbols")
        if self.strategy_mode == "intraday" and len(tickers) > 5:
            raise ValueError("intraday mode supports at most five symbols")
        return tickers

    def guard_monitor(self, *, execute: bool) -> None:
        if self.environment != "paper" or self.live_trading_enabled:
            raise ValueError("monitor supports paper accounts only; live mode is forbidden")
        if self.ib_gateway_port not in (4002, 7497):
            raise ValueError("only standard IBKR paper ports 4002/7497 are allowed")
        if not re.fullmatch(r"DU[0-9]+", self.ib_account_id):
            raise ValueError("monitor requires an explicit DU-prefixed IBKR paper account")
        if execute and not self.paper_trading_enabled:
            raise ValueError("execution also requires AQS_PAPER_TRADING_ENABLED=true")
        if self.allow_short:
            raise ValueError("monitor supports long-only stock accounts")
        if self.agent_target_weight > self.max_position_weight:
            raise ValueError("target weight exceeds the configured position limit")
        if (
            self.notification_webhook
            and not self.notification_webhook.get_secret_value().startswith("https://")
        ):
            raise ValueError("notification webhook must use HTTPS")
        self.watchlist()

    @model_validator(mode="after")
    def guard_live_trading(self) -> Self:
        if (self.environment == "live" or self.live_trading_enabled) and (
            not self.live_trading_enabled or self.live_confirmation != _LIVE_CONFIRMATION
        ):
            raise ValueError(
                "live trading requires AQS_LIVE_TRADING_ENABLED=true and the exact "
                "AQS_LIVE_CONFIRMATION token"
            )
        if self.execution_provider == "nautilus" and not self.ib_account_id:
            raise ValueError("AQS_IB_ACCOUNT_ID is required for Nautilus/IBKR execution")
        return self

    def risk_limits(self) -> RiskLimits:
        return RiskLimits(
            min_confidence=self.min_confidence,
            max_position_weight=self.max_position_weight,
            max_order_notional=self.max_order_notional,
            max_gross_exposure=self.max_gross_exposure,
            max_daily_loss_fraction=self.max_daily_loss_fraction,
            allow_short=self.allow_short,
            allow_fractional_shares=self.allow_fractional_shares,
            max_signal_age_seconds=self.max_signal_age_seconds,
        )
