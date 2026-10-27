from __future__ import annotations

import math
import time
from datetime import UTC, date, datetime
from decimal import Decimal

from agent_quant_system.config import Settings
from agent_quant_system.models import OrderProposal, PortfolioSnapshot, Position
from agent_quant_system.trading.monitor import BrokerSnapshot, OrderUpdate, Quote, market_session


def amount(value) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or abs(number) > Decimal("1e15"):
        raise ValueError("broker returned an unavailable numeric value")
    return number


class IBKRPaperBroker:
    def __init__(self, settings: Settings, *, execute: bool = False, client=None):
        from ib_async import IB

        settings.guard_monitor(execute=execute)
        self.settings, self.execute = settings, execute
        self.ib = client or IB()
        self.ib.RequestTimeout = settings.ib_timeout_seconds
        self.contracts = {}
        self.pnl = None
        self.pnl_updated_at: datetime | None = None
        self.last_snapshot: BrokerSnapshot | None = None
        self.connection_generation = 0
        self.ib.pnlEvent += self._on_pnl

    def _on_pnl(self, pnl):
        if pnl.account == self.settings.ib_account_id:
            self.pnl_updated_at = datetime.now(UTC)

    def assert_paper_account(self):
        self.settings.guard_monitor(execute=self.execute)
        accounts = self.ib.managedAccounts()
        if self.settings.ib_account_id not in accounts or any(
            not item.startswith("DU") for item in accounts
        ):
            raise RuntimeError("connected session does not match the configured paper-only account")

    def connect(self):
        if self.ib.isConnected():
            self.assert_paper_account()
            return
        self.contracts.clear()
        self.pnl = None
        self.pnl_updated_at = None
        self.last_snapshot = None
        try:
            self.ib.connect(
                self.settings.ib_gateway_host,
                self.settings.ib_gateway_port,
                clientId=self.settings.ib_client_id,
                timeout=self.settings.ib_timeout_seconds,
                readonly=not self.execute,
                account=self.settings.ib_account_id,
                raiseSyncErrors=True,
            )
            self.assert_paper_account()
            self.ib.reqMarketDataType(1)
            self.pnl = self.ib.reqPnL(self.settings.ib_account_id)
            self.connection_generation += 1
        except Exception:
            self.ib.disconnect()
            raise

    def disconnect(self):
        self.ib.disconnect()

    def wait(self, seconds: float):
        self.ib.sleep(seconds)

    def contract(self, ticker: str):
        from ib_async import Stock

        if ticker not in self.contracts:
            qualified = self.ib.qualifyContracts(Stock(ticker, "SMART", "USD"))
            if (
                len(qualified) != 1
                or qualified[0].secType != "STK"
                or qualified[0].currency != "USD"
            ):
                raise ValueError(f"ambiguous or unsupported stock contract: {ticker}")
            self.contracts[ticker] = qualified[0]
        return self.contracts[ticker]

    def history(self, ticker: str) -> list[tuple[date, Decimal]]:
        self.connect()
        bars = self.ib.reqHistoricalData(
            self.contract(ticker),
            endDateTime="",
            durationStr="2 M",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
            timeout=self.settings.ib_timeout_seconds,
        )
        return [
            (bar.date.date() if isinstance(bar.date, datetime) else bar.date, amount(bar.close))
            for bar in bars
        ]

    def intraday_history(self, ticker: str) -> list[tuple[datetime, Decimal]]:
        self.connect()
        bars = self.ib.reqHistoricalData(
            self.contract(ticker),
            endDateTime="",
            durationStr="5 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
            timeout=self.settings.ib_timeout_seconds,
        )
        if any(not isinstance(bar.date, datetime) or bar.date.tzinfo is None for bar in bars):
            raise ValueError("intraday bar timestamps must be timezone aware")
        return [(bar.date, amount(bar.close)) for bar in bars]

    def managed_open_orders(self, references: set[str]):
        self.connect()
        orders = [
            trade
            for trade in self.ib.reqAllOpenOrders()
            if trade.order.account == self.settings.ib_account_id and not trade.isDone()
        ]
        if any(
            trade.order.orderRef not in references
            or trade.order.clientId != self.settings.ib_client_id
            for trade in orders
        ):
            raise RuntimeError("foreign or untracked account order requires manual review")
        return orders

    def managed_positions(self) -> dict[str, Decimal]:
        self.connect()
        positions = {}
        for held in self.ib.reqPositions():
            if held.account != self.settings.ib_account_id or not held.position:
                continue
            if (
                held.contract.secType != "STK"
                or held.contract.currency != "USD"
                or held.position < 0
            ):
                raise ValueError("intraday account must contain only long USD stocks")
            if held.contract.symbol in positions:
                raise ValueError("ambiguous account positions")
            positions[held.contract.symbol] = amount(held.position)
        return positions

    def cancel_managed(self, reference: str, references: set[str]):
        self.assert_paper_account()
        if not self.execute:
            raise RuntimeError("read-only broker cannot cancel orders")
        for trade in self.managed_open_orders(references):
            if trade.order.orderRef == reference:
                self.ib.cancelOrder(trade.order)

    def submit_managed(self, order: OrderProposal, references: set[str]):
        from ib_async import LimitOrder, MarketOrder, StopOrder

        self.assert_paper_account()
        if not self.execute:
            raise RuntimeError("read-only broker cannot submit orders")
        if order.role == "daily" or order.client_order_id not in references:
            raise ValueError("intraday orders require a persisted managed identity")
        if order.quantity != int(order.quantity):
            raise ValueError("only whole shares are supported")
        opened = self.managed_open_orders(references)
        if any(trade.order.orderRef == order.client_order_id for trade in opened):
            raise RuntimeError("order is already open; do not resubmit")
        positions = self.managed_positions()
        contract = self.contract(order.ticker)
        now = datetime.now(UTC)
        if not market_session(now)[0]:
            raise RuntimeError("regular trading session has ended")
        common = dict(
            account=self.settings.ib_account_id,
            orderRef=order.client_order_id,
            tif="DAY",
            outsideRth=False,
            transmit=True,
        )
        if order.role == "entry":
            from datetime import timedelta

            from agent_quant_system.trading.intraday import session_bounds

            bounds = session_bounds(now)
            if bounds is None or not bounds[0] + timedelta(minutes=15) <= now < bounds[
                1
            ] - timedelta(minutes=15):
                raise RuntimeError("outside intraday entry window")
            if self.settings.monitor_stop_path.exists():
                raise RuntimeError("stop requested")
            snapshot = self.last_snapshot
            if (
                snapshot is None
                or not 0
                <= (now - snapshot.observed_at).total_seconds()
                <= self.settings.quote_max_age_seconds
            ):
                raise ValueError("fresh account snapshot required")
            for quote in snapshot.quotes.values():
                quote.check(now, self.settings)
            if positions.get(order.ticker, 0) or any(
                trade.contract.symbol == order.ticker for trade in opened
            ):
                raise ValueError("symbol is occupied")
            if any(trade.order.action == "BUY" for trade in opened):
                raise ValueError("another entry is pending")
            if order.estimated_notional > self.settings.max_order_notional:
                raise ValueError("order exceeds notional limit")
            if order.estimated_notional + self.settings.order_cash_buffer > min(
                snapshot.portfolio.cash, snapshot.available_funds
            ):
                raise ValueError("insufficient settled funds")
            request = LimitOrder("BUY", int(order.quantity), float(order.reference_price), **common)
        else:
            reserved = sum(
                (
                    amount(trade.order.totalQuantity) - amount(trade.orderStatus.filled)
                    for trade in opened
                    if trade.contract.symbol == order.ticker and trade.order.action == "SELL"
                ),
                Decimal("0"),
            )
            if order.quantity + reserved > positions.get(order.ticker, Decimal("0")):
                raise ValueError("sell orders exceed actual broker position")
            if order.role == "exit":
                if any(trade.contract.symbol == order.ticker for trade in opened):
                    raise ValueError("exit requires confirmed cancellation of all symbol orders")
                request = MarketOrder("SELL", int(order.quantity), **common)
            else:
                request = StopOrder(
                    "SELL", int(order.quantity), float(order.reference_price), **common
                )
        self.ib.placeOrder(contract, request)

    def snapshot(self, symbols: tuple[str, ...]) -> BrokerSnapshot:
        self.connect()
        account = self.settings.ib_account_id
        self.ib.client.reqAccountUpdates(False, account)
        self.ib.reqAccountUpdates(account)
        positions = [position for position in self.ib.reqPositions() if position.account == account]
        contracts = {ticker: self.contract(ticker) for ticker in symbols}
        for held in positions:
            contract = held.contract
            if contract.secType != "STK" or contract.currency != "USD" or held.position < 0:
                raise ValueError("monitor requires a USD, long-only, stock-only account")
            if held.position:
                contracts.setdefault(contract.symbol, contract)
                if contracts[contract.symbol].conId != contract.conId:
                    raise ValueError("multiple contracts share the same position symbol")
        ticks = self.ib.reqTickers(*contracts.values())
        quotes = {}
        for tick in ticks:
            if tick.time is None or tick.marketDataType != 1:
                raise ValueError("real-time quote permission is required")
            minimum_tick = amount(tick.minTick)
            quotes[tick.contract.symbol] = Quote(
                ticker=tick.contract.symbol,
                bid=amount(tick.bid),
                ask=amount(tick.ask),
                observed_at=tick.time,
                market_data_type=tick.marketDataType,
                minimum_tick=minimum_tick,
            )
        if set(quotes) != set(contracts):
            raise ValueError("incomplete account quotes")
        values = self.ib.accountValues(account)
        currency = next((item.value for item in values if item.tag == "Currency"), None)
        if currency != "USD":
            raise ValueError("USD base currency is required")

        def account_value(tag):
            matches = [
                item for item in values if item.tag == tag and item.currency in ("USD", "BASE")
            ]
            if not matches:
                raise ValueError(f"missing account field {tag}")
            return amount(matches[0].value)

        for item in values:
            if (
                item.tag == "CashBalance"
                and item.currency not in ("USD", "BASE")
                and amount(item.value)
            ):
                raise ValueError("foreign-currency cash positions are not supported")
        deadline = time.monotonic() + self.settings.ib_timeout_seconds
        while self.pnl_updated_at is None and time.monotonic() < deadline:
            self.ib.waitOnUpdate(timeout=min(1, max(0, deadline - time.monotonic())))
        now = datetime.now(UTC)
        if self.pnl_updated_at is None or (now - self.pnl_updated_at).total_seconds() > 30:
            raise ValueError("fresh account-wide daily PnL is required")
        cash = account_value("TotalCashValue")
        net_liquidation = account_value("NetLiquidation")
        available = min(account_value("AvailableFunds"), account_value("SettledCash"))
        daily_pnl = amount(self.pnl.dailyPnL)
        start_equity = net_liquidation - daily_pnl
        if start_equity <= 0 or cash < 0 or available < 0:
            raise ValueError("account funding or daily PnL baseline is invalid")
        book = {
            held.contract.symbol: Position(
                ticker=held.contract.symbol,
                quantity=amount(held.position),
                market_price=quotes[held.contract.symbol].mid,
                average_cost=amount(held.avgCost),
            )
            for held in positions
            if held.position
        }
        if len(book) != sum(bool(held.position) for held in positions):
            raise ValueError("duplicate stock positions cannot be reconciled")
        equity = cash + sum(position.market_value for position in book.values())
        if abs(equity - net_liquidation) > max(
            Decimal("1"), abs(net_liquidation) * Decimal("0.005")
        ):
            raise ValueError("account equity differs from cash plus all stock positions")
        for quote in quotes.values():
            quote.check(now, self.settings)
        self.last_snapshot = BrokerSnapshot(
            portfolio=PortfolioSnapshot(
                cash=cash,
                equity=equity,
                positions=book,
                daily_pnl_fraction=daily_pnl / start_equity,
            ),
            available_funds=available,
            quotes=quotes,
            observed_at=now,
        )
        return self.last_snapshot

    def reconcile(self) -> tuple[dict[str, OrderUpdate], bool]:
        from ib_async import ExecutionFilter

        self.connect()
        opened = self.ib.reqAllOpenOrders()
        completed = self.ib.reqCompletedOrders(apiOnly=False)
        executions = self.ib.reqExecutions(ExecutionFilter(acctCode=self.settings.ib_account_id))
        fills = {}
        for fill in executions:
            execution = fill.execution
            if execution.acctNumber == self.settings.ib_account_id:
                fills.setdefault(execution.orderRef, {})[execution.execId] = execution
        updates = {}
        for trade in [*opened, *completed]:
            order = trade.order
            if order.account != self.settings.ib_account_id or not order.orderRef.startswith(
                "aqs-"
            ):
                continue
            known = fills.get(order.orderRef, {})
            filled = amount(trade.orderStatus.filled)
            average = amount(trade.orderStatus.avgFillPrice)
            executed = sum((amount(item.shares) for item in known.values()), Decimal("0"))
            if executed > filled:
                filled = executed
                average = (
                    sum(amount(item.shares) * amount(item.price) for item in known.values())
                    / filled
                )
            updates[order.orderRef] = OrderUpdate(
                reference=order.orderRef,
                status=trade.orderStatus.status,
                filled_quantity=filled,
                average_price=average,
                executions=tuple(sorted(known)),
            )
        any_open = any(trade.order.account == self.settings.ib_account_id for trade in opened)
        return updates, any_open

    def submit(self, order: OrderProposal):
        from ib_async import LimitOrder

        self.assert_paper_account()
        if not self.execute:
            raise RuntimeError("read-only broker cannot submit orders")
        if not order.client_order_id.startswith("aqs-") or order.quantity != int(order.quantity):
            raise ValueError("only journaled whole-share orders are supported")
        if not math.isfinite(float(order.reference_price)):
            raise ValueError("invalid order price")
        _, opened = self.reconcile()
        if opened:
            raise RuntimeError("account has an open order; submission refused")
        contract = self.contract(order.ticker)
        now = datetime.now(UTC)
        if self.settings.monitor_stop_path.exists() or not market_session(now)[0]:
            raise RuntimeError("stop requested or regular trading session has ended")
        snapshot = self.last_snapshot
        if (
            snapshot is None
            or not 0
            <= (now - snapshot.observed_at).total_seconds()
            <= self.settings.quote_max_age_seconds
        ):
            raise RuntimeError("a fresh account snapshot is required before submission")
        for quote in snapshot.quotes.values():
            quote.check(now, self.settings)
        if order.estimated_notional > self.settings.max_order_notional:
            raise ValueError("order exceeds the notional limit")
        if order.action.value == "SELL":
            held = snapshot.portfolio.position_for(order.ticker)
            if held is None or order.quantity > held.quantity:
                raise ValueError("sell quantity exceeds the actual position")
        elif order.estimated_notional + self.settings.order_cash_buffer > min(
            snapshot.portfolio.cash,
            snapshot.available_funds,
        ):
            raise ValueError("insufficient settled funds with cash buffer")
        request = LimitOrder(
            order.action.value,
            int(order.quantity),
            float(order.reference_price),
            account=self.settings.ib_account_id,
            orderRef=order.client_order_id,
            tif="DAY",
            outsideRth=False,
            transmit=True,
        )
        self.ib.placeOrder(contract, request)

    def cancel_own_orders(self):
        self.connect()
        if not self.execute:
            return
        for trade in self.ib.reqAllOpenOrders():
            order = trade.order
            if (
                order.account == self.settings.ib_account_id
                and order.orderRef.startswith("aqs-")
                and order.clientId == self.settings.ib_client_id
            ):
                self.ib.cancelOrder(order)
