from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_quant_system.config import Settings
from agent_quant_system.models import OrderProposal
from agent_quant_system.trading.monitor import TERMINAL, Journal, OrderUpdate


class TrackedOrder(BaseModel):
    proposal: OrderProposal
    created: datetime
    status: Literal[
        "PLANNED",
        "INTENT",
        "UNKNOWN",
        "PendingSubmit",
        "ApiPending",
        "PreSubmitted",
        "Submitted",
        "PendingCancel",
        "Filled",
        "Cancelled",
        "ApiCancelled",
        "Inactive",
    ] = "PLANNED"
    filled: Decimal = Field(default=Decimal("0"), ge=0)
    average: Decimal = Field(default=Decimal("0"), ge=0)
    cancel_requested_at: datetime | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.created.tzinfo is None or self.filled > self.proposal.quantity:
            raise ValueError("invalid persisted order state")
        if self.filled != self.filled.to_integral_value():
            raise ValueError("fractional intraday fills require manual review")
        if self.status == "Filled" and self.filled != self.proposal.quantity:
            raise ValueError("filled order has inconsistent quantity")
        if self.filled and self.average <= 0:
            raise ValueError("filled order needs a valid average price")
        return self

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL


class IntradayState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    session: date | None = None
    opening_equity: Decimal = Field(default=Decimal("0"), ge=0)
    entry_signals: list[str] = Field(default_factory=list)
    halt_reason: str = ""
    orders: dict[str, TrackedOrder] = Field(default_factory=dict)
    exiting: set[str] = Field(default_factory=set)
    exit_blocked: set[str] = Field(default_factory=set)
    last_bars: dict[str, datetime] = Field(default_factory=dict)
    tick_sizes: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ledger_consistent(self) -> Self:
        if len(set(self.entry_signals)) != len(self.entry_signals):
            raise ValueError("duplicate persisted entry signal")
        for reference, order in self.orders.items():
            if reference != order.proposal.client_order_id or order.proposal.role == "daily":
                raise ValueError("invalid persisted order identity")
        if any(self.position(symbol) < 0 for symbol in self.symbols()):
            raise ValueError("persisted ledger contains a short position")
        if self.session and self.opening_equity <= 0:
            raise ValueError("persisted session needs a positive opening equity")
        if any(not tick.is_finite() or tick <= 0 for tick in self.tick_sizes.values()):
            raise ValueError("invalid persisted tick size")
        return self

    def symbols(self) -> set[str]:
        return {order.proposal.ticker for order in self.orders.values()}

    def position(self, ticker: str) -> Decimal:
        return sum(
            (
                order.filled * (1 if order.proposal.action.value == "BUY" else -1)
                for order in self.orders.values()
                if order.proposal.ticker == ticker
            ),
            Decimal("0"),
        )

    def active(self, ticker: str | None = None) -> list[TrackedOrder]:
        return [
            order
            for order in self.orders.values()
            if order.active and (ticker is None or order.proposal.ticker == ticker)
        ]

    def add(
        self, ticker: str, role: str, quantity: Decimal, price: Decimal, now: datetime
    ) -> TrackedOrder:
        proposal = OrderProposal(
            ticker=ticker,
            action="BUY" if role == "entry" else "SELL",
            quantity=quantity,
            reference_price=price,
            role=role,
            order_type={"entry": "LMT", "stop": "STP", "exit": "MKT"}[role],
            rationale=f"intraday SMA9/21 {role}",
        )
        proposal = proposal.model_copy(
            update={"client_order_id": "aqs-" + proposal.client_order_id[:24]}
        )
        tracked = TrackedOrder(proposal=proposal, created=now)
        self.orders[proposal.client_order_id] = tracked
        return tracked

    def reconcile(
        self,
        updates: dict[str, OrderUpdate],
        now: datetime,
        stop_fraction: Decimal,
        ticks: dict[str, Decimal],
    ):
        for reference, tracked in list(self.orders.items()):
            if tracked.status == "PLANNED":
                continue
            update = updates.get(reference)
            if update is None:
                if tracked.active:
                    tracked.status = "UNKNOWN"
                continue
            replacement = TrackedOrder(
                proposal=tracked.proposal,
                created=tracked.created,
                status=update.status,
                filled=update.filled_quantity,
                average=update.average_price,
                cancel_requested_at=tracked.cancel_requested_at,
            )
            delta = replacement.filled - tracked.filled
            if delta < 0 or (not tracked.active and replacement.active):
                raise ValueError("broker order state regressed")
            if not delta and replacement.average != tracked.average:
                raise ValueError("broker execution correction requires manual reconciliation")
            symbol, role = tracked.proposal.ticker, tracked.proposal.role
            self.orders[reference] = replacement
            if replacement.status == "Inactive":
                self.halt_reason = f"{role}_rejected"
                self.exiting.add(symbol)
                if role == "exit":
                    self.exit_blocked.add(symbol)
            if role == "stop" and (
                delta
                or (
                    replacement.status in {"Cancelled", "ApiCancelled"}
                    and tracked.cancel_requested_at is None
                )
            ):
                self.exiting.add(symbol)
            if role == "entry" and delta and symbol not in self.exiting:
                fill_price = (
                    replacement.filled * replacement.average - tracked.filled * tracked.average
                ) / delta
                tick = ticks[symbol]
                stop = (fill_price * (1 - stop_fraction) / tick).to_integral_value(
                    rounding=ROUND_FLOOR
                ) * tick
                if fill_price <= 0 or stop <= 0:
                    raise ValueError("invalid incremental fill price")
                self.add(symbol, "stop", delta, stop, now)
        self.ledger_consistent()

    def request_exit(self, ticker: str):
        self.exiting.add(ticker)

    def advance(self, now: datetime, prices: dict[str, Decimal], entry_timeout: int):
        for tracked in list(self.orders.values()):
            symbol = tracked.proposal.ticker
            timed_out = (
                tracked.proposal.role == "entry"
                and (now - tracked.created).total_seconds() >= entry_timeout
            )
            if (
                tracked.active
                and tracked.proposal.role != "exit"
                and (symbol in self.exiting or timed_out)
            ):
                if tracked.status == "PLANNED":
                    tracked.status = "Cancelled"
                elif tracked.cancel_requested_at is None:
                    tracked.cancel_requested_at = now
        for symbol in list(self.exiting):
            if self.active(symbol) or symbol in self.exit_blocked:
                continue
            quantity = self.position(symbol)
            if quantity:
                self.add(symbol, "exit", quantity, prices[symbol], now)
            else:
                self.exiting.remove(symbol)

    def save(self, journal: Journal):
        self.ledger_consistent()
        with journal.atomic():
            journal.set("intraday_state", self.model_dump_json())
            for reference, tracked in self.orders.items():
                journal.claim(reference, tracked.proposal)
                journal.db.execute(
                    "UPDATE orders SET status=?,filled=?,average_price=? WHERE reference=?",
                    (tracked.status, str(tracked.filled), str(tracked.average), reference),
                )

    @classmethod
    def load(cls, journal: Journal) -> Self:
        payload = journal.get("intraday_state")
        if payload is None and journal.orders():
            raise ValueError("missing intraday state with existing orders")
        state = cls.model_validate_json(payload) if payload else cls()
        if {row["reference"] for row in journal.orders()} != set(state.orders):
            raise ValueError("intraday state and order journal do not match")
        for row in journal.orders():
            tracked = state.orders[row["reference"]]
            if (
                OrderProposal.model_validate_json(row["payload"]) != tracked.proposal
                or row["status"] != tracked.status
                or Decimal(row["filled"]) != tracked.filled
                or Decimal(row["average_price"]) != tracked.average
            ):
                raise ValueError("intraday state conflicts with the order journal")
        return state


def session_bounds(now: datetime) -> tuple[datetime, datetime] | None:
    import exchange_calendars as xcals
    import pandas as pd

    calendar = xcals.get_calendar("XNYS")
    local_day = pd.Timestamp(now).tz_convert("America/New_York").date()
    if not calendar.is_session(local_day):
        return None
    return (
        calendar.session_open(local_day).to_pydatetime(),
        calendar.session_close(local_day).to_pydatetime(),
    )


def crossover(bars: list[tuple[datetime, Decimal]], now: datetime) -> tuple[datetime, str]:
    import exchange_calendars as xcals
    import pandas as pd

    if any(stamp.tzinfo is None or not price.is_finite() or price <= 0 for stamp, price in bars):
        raise ValueError("invalid intraday bars")
    completed = [(stamp, price) for stamp, price in bars if stamp + timedelta(minutes=5) <= now]
    stamps = [stamp for stamp, _ in completed]
    if len(stamps) < 22 or stamps != sorted(set(stamps)):
        raise ValueError("intraday history needs ordered, unique completed bars")
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(stamps[-22].date(), now.date())
    expected = [
        stamp.to_pydatetime()
        for session in sessions
        for stamp in pd.date_range(
            calendar.session_open(session),
            calendar.session_close(session),
            freq="5min",
            inclusive="left",
        )
        if stamp.to_pydatetime() + timedelta(minutes=5) <= now
    ]
    if stamps[-22:] != expected[-22:]:
        raise ValueError("intraday history has missing regular-session bars")
    closes = pd.Series([float(price) for _, price in completed[-22:]])
    bullish = closes.rolling(9).mean() > closes.rolling(21).mean()
    action = "HOLD"
    if not bullish.iloc[-2] and bullish.iloc[-1]:
        action = "BUY"
    elif bullish.iloc[-2] and not bullish.iloc[-1]:
        action = "SELL"
    return stamps[-1] + timedelta(minutes=5), action


class IntradayMonitor:
    def __init__(self, settings: Settings, broker, journal: Journal, *, execute: bool = False):
        settings.guard_monitor(execute=execute)
        if settings.strategy_mode != "intraday":
            raise ValueError("intraday runner requires intraday strategy mode")
        self.settings, self.broker, self.journal, self.execute = settings, broker, journal, execute
        journal.bind_account(settings.ib_account_id)
        journal.bind_mode("intraday")
        self.state = IntradayState.load(journal)
        self.generation = None
        self.cancel_sent: set[str] = set()
        self.history_cache: dict[str, tuple[int, list]] = {}
        self.entries_allowed = True

    def _save(self):
        if self.execute:
            self.state.save(self.journal)

    def _dispatch(self, now: datetime, *, entries: bool = False):
        if not self.execute:
            return
        references = set(self.state.orders)
        for reference, tracked in self.state.orders.items():
            if tracked.active and tracked.cancel_requested_at and reference not in self.cancel_sent:
                self._save()
                self.broker.cancel_managed(reference, references)
                self.cancel_sent.add(reference)
        for tracked in self.state.orders.values():
            if tracked.status != "PLANNED" or tracked.cancel_requested_at:
                continue
            if tracked.proposal.role == "entry" and (
                not entries
                or self.journal.get("halted")
                or self.settings.monitor_stop_path.exists()
                or (now - tracked.created).total_seconds() > 90
            ):
                tracked.status = "Cancelled"
                self._save()
                continue
            tracked.status = "INTENT"
            self._save()
            self.broker.submit_managed(tracked.proposal, references)
            self.journal.event("intraday_submitted", tracked.proposal.model_dump(mode="json"))

    def _sync(self, now: datetime):
        self.broker.connect()
        generation = self.broker.connection_generation
        if self.generation is not None and generation != self.generation:
            self.journal.set("halted", "intraday_reconnected_review_required")
        self.generation = generation
        updates, _ = self.broker.reconcile()
        self.broker.managed_open_orders(set(self.state.orders))
        self.state.reconcile(
            updates, now, self.settings.intraday_stop_fraction, self.state.tick_sizes
        )
        self._save()

    def stop(self):
        if not self.journal.get("halted"):
            self.journal.set("halted", "operator_stop")
        now = datetime.now(UTC)
        self._sync(now)
        for tracked in self.state.active():
            if tracked.proposal.role == "entry":
                if tracked.status == "PLANNED":
                    tracked.status = "Cancelled"
                elif tracked.cancel_requested_at is None:
                    tracked.cancel_requested_at = now
        self._save()
        self._dispatch(now)
        self.journal.event(
            "intraday_stopped",
            {"protection_preserved": True, "manual_broker_review_required": True},
        )

    def _prices(self) -> dict[str, Decimal]:
        return {
            tracked.proposal.ticker: tracked.average or tracked.proposal.reference_price
            for tracked in self.state.orders.values()
        }

    def _new_session(self, now: datetime, equity: Decimal) -> bool:
        from zoneinfo import ZoneInfo

        session = now.astimezone(ZoneInfo("America/New_York")).date()
        if self.state.session == session:
            return True
        if self.state.session and session < self.state.session:
            raise ValueError("cannot move intraday session backwards")
        if self.state.active() or any(
            self.state.position(symbol) for symbol in self.state.symbols()
        ):
            self.state.halt_reason = "overnight_exposure"
            self.state.exiting.update(self.state.symbols())
            return False
        self.state.session = session
        self.state.opening_equity = equity
        self.state.entry_signals.clear()
        self.state.halt_reason = ""
        self.state.last_bars.clear()
        return True

    def cycle(self, now: datetime | None = None) -> str:
        supplied_clock = now
        now = now or datetime.now(UTC)
        self._sync(now)
        flatten = self.journal.get("flatten_requested") == "true"
        if flatten and not self.execute:
            self.journal.event("intraday_blocked", {"reason": "flatten_requires_execution_mode"})
            return "BLOCKED"
        if flatten:
            self.state.halt_reason = "operator_flatten"
            self.state.exiting.update(self.state.symbols())
        if (self.settings.monitor_stop_path.exists() or self.journal.get("halted")) and not flatten:
            self.stop()
            return "HALTED"
        bounds = session_bounds(now)
        if bounds is None or not bounds[0] <= now < bounds[1]:
            if self.state.active() or any(
                self.state.position(symbol) for symbol in self.state.symbols()
            ):
                self.journal.event("intraday_close_audit_failed", {"manual_review_required": True})
            return "CLOSED"
        self._dispatch(now)
        for order in self.state.active():
            started = order.cancel_requested_at or (
                order.created
                if order.proposal.role == "exit"
                or order.status
                in {"INTENT", "UNKNOWN", "PendingSubmit", "ApiPending", "PendingCancel"}
                else None
            )
            if started and (now - started).total_seconds() > self.settings.intraday_ack_timeout:
                self.journal.set("halted", "intraday_acknowledgement_timeout")
                self.stop()
                return "HALTED"
        if any(order.status == "UNKNOWN" for order in self.state.active()):
            self.journal.event("intraday_blocked", {"reason": "unknown_order"})
            return "BLOCKED"
        try:
            snapshot = self.broker.snapshot(self.settings.watchlist())
        except Exception:
            self.state.halt_reason = "account_or_quotes_unavailable"
            self.state.exiting.update(self.state.symbols())
            self.state.advance(now, self._prices(), self.settings.intraday_entry_timeout)
            self._save()
            self._dispatch(now)
            raise
        clock = supplied_clock or datetime.now(UTC)
        for quote in snapshot.quotes.values():
            quote.check(clock, self.settings)
        if (
            not 0
            <= (clock - snapshot.observed_at).total_seconds()
            <= self.settings.quote_max_age_seconds
        ):
            raise ValueError("stale intraday account snapshot")
        expected = {
            symbol: self.state.position(symbol)
            for symbol in self.state.symbols()
            if self.state.position(symbol)
        }
        actual = {
            symbol: position.quantity
            for symbol, position in snapshot.portfolio.positions.items()
            if position.quantity
        }
        if self.execute and expected != actual:
            raise ValueError("intraday position reconciliation failed; dedicated account required")
        self.state.tick_sizes.update(
            {symbol: quote.minimum_tick for symbol, quote in snapshot.quotes.items()}
        )
        self._new_session(clock, snapshot.portfolio.equity)
        if flatten:
            self.state.halt_reason = "operator_flatten"
        threshold = min(self.settings.max_daily_loss_fraction, self.settings.intraday_daily_loss)
        if (
            snapshot.portfolio.equity <= self.state.opening_equity * (1 - threshold)
            or snapshot.portfolio.daily_pnl_fraction <= -threshold
        ):
            self.state.halt_reason = "daily_loss_limit"
        if clock >= bounds[1] - timedelta(minutes=5):
            self.state.halt_reason = "session_closed"
        if clock >= bounds[1] - timedelta(minutes=1) and (actual or self.state.active()):
            self.journal.event("intraday_close_audit_failed", {"manual_review_required": True})
        if self.state.halt_reason:
            self.state.exiting.update(self.state.symbols())
        prices = {symbol: quote.mid for symbol, quote in snapshot.quotes.items()}
        self.state.advance(clock, prices, self.settings.intraday_entry_timeout)
        self._save()
        self._dispatch(clock)
        if self.state.halt_reason:
            self.journal.event("intraday_risk_halt", {"reason": self.state.halt_reason})
            if (
                flatten
                and not self.state.active()
                and not any(self.state.position(symbol) for symbol in self.state.symbols())
            ):
                self.journal.set("halted", "operator_flatten_complete")
                self.journal.db.execute("DELETE FROM meta WHERE key='flatten_requested'")
                return "HALTED"
            return "RISK_HALTED"
        entry_window = (
            bounds[0] + timedelta(minutes=15) <= clock < bounds[1] - timedelta(minutes=15)
        )
        for symbol in self.settings.watchlist():
            bucket = int(clock.timestamp()) // 300
            cached = self.history_cache.get(symbol)
            if cached is None or cached[0] != bucket:
                cached = (bucket, self.broker.intraday_history(symbol))
                self.history_cache[symbol] = cached
            signal_time, action = crossover(cached[1], clock)
            if not 0 <= (clock - signal_time).total_seconds() <= 90:
                continue
            if self.state.last_bars.get(symbol) == signal_time:
                continue
            self.state.last_bars[symbol] = signal_time
            self.journal.event(
                "intraday_signal",
                {"ticker": symbol, "action": action, "bar_end": signal_time.isoformat()},
            )
            if action == "SELL":
                self.state.request_exit(symbol)
                self.state.advance(clock, prices, self.settings.intraday_entry_timeout)
                self._save()
                self._dispatch(clock)
            elif action == "BUY" and self.execute and entry_window:
                if self._entry(symbol, signal_time, snapshot, clock):
                    return "SUBMITTED"
        self._save()
        return "MONITORED" if entry_window else "OUTSIDE_ENTRY_WINDOW"

    def _entry(self, symbol, signal_time, snapshot, now) -> bool:
        if not self.entries_allowed:
            return False
        if self.state.position(symbol) or self.state.active(symbol) or symbol in self.state.exiting:
            return False
        if any(order.proposal.role == "entry" for order in self.state.active()):
            return False
        occupied = sum(
            bool(self.state.position(ticker) or self.state.active(ticker))
            for ticker in self.state.symbols()
        )
        if occupied >= self.settings.intraday_max_positions:
            return False
        key = f"{symbol}:{signal_time.isoformat()}"
        if (
            key in self.state.entry_signals
            or len(self.state.entry_signals) >= self.settings.intraday_max_entries
        ):
            return False
        quote = snapshot.quotes[symbol]
        if (quote.ask - quote.bid) / quote.mid * 10000 > min(
            self.settings.max_spread_bps, Decimal("10")
        ):
            return False
        price = (quote.ask * Decimal("1.0002") / quote.minimum_tick).to_integral_value(
            rounding=ROUND_CEILING
        ) * quote.minimum_tick
        equity = snapshot.portfolio.equity
        budget = min(
            equity * min(self.settings.max_position_weight, Decimal("0.10")),
            equity * min(self.settings.max_gross_exposure, self.settings.intraday_max_gross)
            - snapshot.portfolio.gross_exposure,
            min(snapshot.portfolio.cash, snapshot.available_funds)
            - equity * self.settings.intraday_cash_reserve
            - self.settings.order_cash_buffer,
            self.settings.max_order_notional,
        )
        quantity = (budget / price).to_integral_value(rounding=ROUND_FLOOR)
        if quantity <= 0 or quantity * price < 100:
            return False
        self.state.entry_signals.append(key)
        self.state.add(symbol, "entry", quantity, price, now)
        self._save()
        self._dispatch(now, entries=True)
        return True
