from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_quant_system.config import Settings
from agent_quant_system.models import AgentSignal, OrderProposal, PortfolioSnapshot, TradeAction
from agent_quant_system.risk import RiskEngine

TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True)
    ticker: str
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    observed_at: datetime
    market_data_type: int = 1
    minimum_tick: Decimal = Field(default=Decimal("0.01"), gt=0)

    @model_validator(mode="after")
    def valid_market(self):
        if self.ask < self.bid or self.observed_at.tzinfo is None:
            raise ValueError("quote must have a valid spread and timezone")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    def check(self, now: datetime, settings: Settings) -> None:
        age = (now - self.observed_at).total_seconds()
        if self.market_data_type != 1 or not 0 <= age <= settings.quote_max_age_seconds:
            raise ValueError("missing, delayed or stale real-time quote")
        if (self.ask - self.bid) / self.mid * 10000 > settings.max_spread_bps:
            raise ValueError("quote spread exceeds limit")


@dataclass(frozen=True)
class BrokerSnapshot:
    portfolio: PortfolioSnapshot
    available_funds: Decimal
    quotes: dict[str, Quote]
    observed_at: datetime


@dataclass(frozen=True)
class OrderUpdate:
    reference: str
    status: str
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    executions: tuple[str, ...] = ()


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                reference TEXT PRIMARY KEY, signal_key TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL,
                filled TEXT NOT NULL DEFAULT '0', average_price TEXT NOT NULL DEFAULT '0',
                executions TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def atomic(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def bind_mode(self, mode: str):
        with self.atomic():
            existing = self.get("strategy_mode")
            if existing is None and self.orders():
                existing = "daily"
            if existing is not None and existing != mode:
                raise ValueError("state database belongs to another strategy mode")
            self.set("strategy_mode", mode)

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def bind_account(self, account: str):
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('account',?)", (account,))
        if self.get("account") != account:
            raise ValueError("state database belongs to a different account")

    def event(self, kind: str, payload: dict):
        self.db.execute(
            "INSERT INTO events(created,kind,payload) VALUES (?,?,?)",
            (datetime.now(UTC).isoformat(), kind, json.dumps(payload, default=str)),
        )

    def claim(self, key: str, order: OrderProposal) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO orders(reference,signal_key,status,payload,updated) "
            "VALUES (?,?,'INTENT',?,?)",
            (order.client_order_id, key, order.model_dump_json(), datetime.now(UTC).isoformat()),
        )
        return cursor.rowcount == 1

    def orders(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM orders ORDER BY updated")]

    def active(self) -> list[dict]:
        return [row for row in self.orders() if row["status"] not in TERMINAL]

    def reconcile(self, updates: dict[str, OrderUpdate]):
        for row in self.orders():
            update = updates.get(row["reference"])
            if update is None:
                if row["status"] not in TERMINAL:
                    self.db.execute(
                        "UPDATE orders SET status='UNKNOWN' WHERE reference=?", (row["reference"],)
                    )
                continue
            if update.filled_quantity < Decimal(row["filled"]):
                raise ValueError("broker fill quantity regressed")
            order = OrderProposal.model_validate_json(row["payload"])
            if (
                not update.filled_quantity.is_finite()
                or not 0 <= update.filled_quantity <= order.quantity
            ):
                raise ValueError("broker filled quantity is invalid")
            if update.status == "Filled" and update.filled_quantity != order.quantity:
                raise ValueError("filled status does not reconcile with requested quantity")
            if row["status"] in TERMINAL and update.status not in TERMINAL:
                raise ValueError("terminal order status regressed")
            changed = (
                row["status"] != update.status or Decimal(row["filled"]) != update.filled_quantity
            )
            self.db.execute(
                "UPDATE orders SET status=?,filled=?,average_price=?,executions=?,updated=? "
                "WHERE reference=?",
                (
                    update.status,
                    str(update.filled_quantity),
                    str(update.average_price),
                    json.dumps(sorted(set(json.loads(row["executions"])) | set(update.executions))),
                    datetime.now(UTC).isoformat(),
                    update.reference,
                ),
            )
            if changed:
                self.event(
                    "order_update",
                    {
                        "reference": update.reference,
                        "status": update.status,
                        "filled": str(update.filled_quantity),
                    },
                )


@contextmanager
def monitor_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another monitor is using this state database") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def deliver_notifications(journal: Journal, settings: Settings):
    rows = journal.db.execute(
        "SELECT * FROM events WHERE delivered=0 ORDER BY id LIMIT 100"
    ).fetchall()
    for row in rows:
        message = {
            "event_id": row["id"],
            "time": row["created"],
            "kind": row["kind"],
            "data": json.loads(row["payload"]),
        }
        print(json.dumps(message), file=sys.stderr, flush=True)
        if settings.notification_webhook:
            request = Request(
                settings.notification_webhook.get_secret_value(),
                data=json.dumps(message).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("notification delivery failed")
        journal.db.execute("UPDATE events SET delivered=1 WHERE id=?", (row["id"],))


def market_session(now: datetime) -> tuple[bool, date]:
    import exchange_calendars as xcals
    import pandas as pd

    calendar = xcals.get_calendar("XNYS")
    minute = pd.Timestamp(now).floor("min")
    return bool(calendar.is_open_on_minute(minute)), minute.tz_convert("America/New_York").date()


def sma_signal(
    ticker: str, bars: list[tuple[date, Decimal]], now: datetime, settings: Settings
) -> AgentSignal:
    import exchange_calendars as xcals
    import pandas as pd

    calendar = xcals.get_calendar("XNYS")
    local_day = pd.Timestamp(now).tz_convert("America/New_York").date()
    session = calendar.date_to_session(local_day, direction="previous")
    if calendar.session_close(session) >= pd.Timestamp(now):
        session = calendar.previous_session(session)
    expected = calendar.sessions_window(session, -19)
    values = dict(bars)
    if len(values) != len(bars):
        raise ValueError("duplicate daily bars")
    closes = [values[stamp.date()] for stamp in expected]
    if any(not value.is_finite() or value <= 0 for value in closes):
        raise ValueError("invalid daily history")
    bullish = closes[-1] > sum(closes) / len(closes)
    return AgentSignal(
        ticker=ticker,
        action=TradeAction.BUY if bullish else TradeAction.SELL,
        confidence=Decimal("1"),
        target_weight=settings.agent_target_weight if bullish else Decimal("0"),
        thesis=f"SMA20 baseline through {session.date()}; not an LLM confidence estimate",
        sources=("IBKR/TRADES/RTH/SMA20",),
        generated_at=now,
    )


class Monitor:
    def __init__(self, settings: Settings, broker, journal: Journal, *, execute: bool = False):
        settings.guard_monitor(execute=execute)
        self.settings, self.broker, self.journal, self.execute = settings, broker, journal, execute
        journal.bind_account(settings.ib_account_id)
        journal.bind_mode("daily")
        self.risk = RiskEngine(
            settings.risk_limits().model_copy(
                update={"allow_fractional_shares": False},
            )
        )

    def stop(self):
        self.journal.set("halted", "operator_stop")
        self.broker.cancel_own_orders()
        updates, _ = self.broker.reconcile()
        self.journal.reconcile(updates)
        self.journal.event("stopped", {"unresolved_orders": len(self.journal.active())})

    def cycle(self, now: datetime | None = None) -> str:
        supplied_clock = now
        now = now or datetime.now(UTC)
        if self.settings.monitor_stop_path.exists() or self.journal.get("halted"):
            self.stop()
            return "HALTED"
        updates, external_open = self.broker.reconcile()
        self.journal.reconcile(updates)
        if external_open or self.journal.active():
            self.journal.event("blocked", {"reason": "open_or_unresolved_orders"})
            return "BLOCKED"
        opened, session = market_session(now)
        if not opened:
            self.journal.event("market_closed", {"session": session.isoformat()})
            return "CLOSED"
        for ticker in self.settings.watchlist():
            bars = self.broker.history(ticker)
            snapshot = self.broker.snapshot(self.settings.watchlist())
            clock = supplied_clock or datetime.now(UTC)
            if (
                not 0
                <= (clock - snapshot.observed_at).total_seconds()
                <= self.settings.quote_max_age_seconds
            ):
                raise ValueError("stale account snapshot")
            for quote in snapshot.quotes.values():
                quote.check(clock, self.settings)
            quote = snapshot.quotes[ticker]
            signal = sma_signal(ticker, bars, clock, self.settings)
            offset = self.settings.order_limit_offset_bps / 10000
            price = (
                quote.ask * (1 + offset)
                if signal.action is TradeAction.BUY
                else quote.bid * (1 - offset)
            )
            rounding = ROUND_CEILING if signal.action is TradeAction.BUY else ROUND_FLOOR
            price = (price / quote.minimum_tick).to_integral_value(
                rounding=rounding
            ) * quote.minimum_tick
            decision = self.risk.evaluate(signal, snapshot.portfolio, price, now=clock)
            self.journal.event(
                "assessment",
                {
                    "ticker": ticker,
                    "signal": signal.model_dump(mode="json"),
                    "risk": decision.model_dump(mode="json"),
                },
            )
            if not self.execute or not decision.approved:
                continue
            order = decision.order
            if order.action is TradeAction.BUY and (
                order.estimated_notional + self.settings.order_cash_buffer
                > min(snapshot.portfolio.cash, snapshot.available_funds)
            ):
                self.journal.event("blocked", {"ticker": ticker, "reason": "cash_buffer"})
                continue
            key = f"{self.settings.ib_account_id}:{session}:{ticker}:SMA20-v1"
            reference = "aqs-" + hashlib.sha256(key.encode()).hexdigest()[:24]
            order = order.model_copy(update={"client_order_id": reference})
            if self.settings.monitor_stop_path.exists() or self.journal.get("halted"):
                self.stop()
                return "HALTED"
            updates, external_open = self.broker.reconcile()
            self.journal.reconcile(updates)
            if external_open or self.journal.active():
                return "BLOCKED"
            submission_clock = supplied_clock or datetime.now(UTC)
            if not market_session(submission_clock)[0]:
                return "CLOSED"
            for latest_quote in snapshot.quotes.values():
                latest_quote.check(submission_clock, self.settings)
            if self.settings.monitor_stop_path.exists() or self.journal.get("halted"):
                self.stop()
                return "HALTED"
            if not self.journal.claim(key, order):
                continue
            self.broker.submit(order)
            self.journal.event("submitted", {"reference": reference, "ticker": ticker})
            updates, _ = self.broker.reconcile()
            self.journal.reconcile(updates)
            return "SUBMITTED"
        return "MONITORED"
