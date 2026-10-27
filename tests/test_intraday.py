from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent_quant_system.config import Settings
from agent_quant_system.models import OrderProposal, PortfolioSnapshot, Position
from agent_quant_system.trading.intraday import IntradayMonitor, IntradayState
from agent_quant_system.trading.monitor import TERMINAL, BrokerSnapshot, Journal, OrderUpdate, Quote


def proposal(**kwargs):
    return OrderProposal(
        ticker="SPY", action="SELL", quantity=2, reference_price=100, rationale="test", **kwargs
    )


def test_execution_roles_are_explicit_and_validated():
    assert proposal().order_type == "LMT"
    stop = proposal(role="stop", order_type="STP")
    assert OrderProposal.model_validate_json(stop.model_dump_json()) == stop
    with pytest.raises(ValueError, match="execution role"):
        proposal(role="stop")


def test_state_and_order_intents_commit_or_rollback_together(tmp_path):
    journal = Journal(tmp_path / "state.db")
    journal.bind_mode("intraday")
    with pytest.raises(RuntimeError), journal.atomic():
        journal.claim("fill-1", proposal(role="stop", order_type="STP"))
        journal.set("intraday_state", '{"protected": 2}')
        raise RuntimeError("crash before commit")
    assert not journal.orders()
    assert journal.get("intraday_state") is None
    with journal.atomic():
        journal.claim("fill-1", proposal(role="stop", order_type="STP"))
        journal.set("intraday_state", '{"protected": 2}')
    journal.close()
    restored = Journal(tmp_path / "state.db")
    assert len(restored.orders()) == 1
    assert Decimal(restored.orders()[0]["filled"]) == 0
    assert restored.get("intraday_state") == '{"protected": 2}'
    with pytest.raises(ValueError, match="strategy mode"):
        restored.bind_mode("daily")
    restored.close()


NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)


def entered_state():
    state = IntradayState()
    entry = state.add("SPY", "entry", Decimal("10"), Decimal("100"), NOW)
    entry.status = "INTENT"
    return state, entry.proposal.client_order_id


def reconcile(state, reference, status, quantity, average="100"):
    state.reconcile(
        {reference: OrderUpdate(reference, status, Decimal(quantity), Decimal(average))},
        NOW,
        Decimal("0.005"),
        {"SPY": Decimal("0.01")},
    )


def test_partial_fills_protect_only_the_increment_and_survive_restart(tmp_path):
    state, reference = entered_state()
    reconcile(state, reference, "Submitted", "4")
    assert state.position("SPY") == 4
    assert [item.proposal.quantity for item in state.active() if item.proposal.role == "stop"] == [
        4
    ]
    journal = Journal(tmp_path / "state.db")
    state.save(journal)
    restored = IntradayState.load(journal)
    reconcile(restored, reference, "Submitted", "4")
    assert len(restored.orders) == 2
    reconcile(restored, reference, "Filled", "10", "101")
    stops = [item for item in restored.orders.values() if item.proposal.role == "stop"]
    assert [item.proposal.quantity for item in stops] == [4, 6]
    assert stops[1].proposal.reference_price == Decimal("101.15")
    journal.close()


def test_exit_waits_for_cancellation_and_accounts_for_late_fill():
    state, reference = entered_state()
    reconcile(state, reference, "Submitted", "4")
    stop = next(item for item in state.orders.values() if item.proposal.role == "stop")
    stop.status = "Submitted"
    state.request_exit("SPY")
    state.advance(NOW, {"SPY": Decimal("100")}, 60)
    assert not any(item.proposal.role == "exit" for item in state.orders.values())
    reconcile(state, reference, "Cancelled", "6")
    state.advance(NOW, {"SPY": Decimal("100")}, 60)
    assert not any(item.proposal.role == "exit" for item in state.orders.values())
    reconcile(state, stop.proposal.client_order_id, "Cancelled", "0", "0")
    state.advance(NOW, {"SPY": Decimal("100")}, 60)
    exits = [item for item in state.orders.values() if item.proposal.role == "exit"]
    assert len(exits) == 1 and exits[0].proposal.quantity == 6


def test_unknown_submission_and_rejected_exit_are_not_retried():
    state, reference = entered_state()
    state.reconcile({}, NOW, Decimal("0.005"), {})
    assert state.orders[reference].status == "UNKNOWN"
    state.request_exit("SPY")
    state.advance(NOW + timedelta(seconds=61), {"SPY": Decimal("100")}, 60)
    assert len(state.orders) == 1
    reconcile(state, reference, "Filled", "10")
    state.advance(NOW, {"SPY": Decimal("100")}, 60)
    exit_order = next(item for item in state.orders.values() if item.proposal.role == "exit")
    exit_order.status = "INTENT"
    reconcile(state, exit_order.proposal.client_order_id, "Inactive", "0", "0")
    state.advance(NOW, {"SPY": Decimal("100")}, 60)
    assert len(state.orders) == 2
    assert "SPY" in state.exit_blocked


def test_corrupt_state_does_not_reset_risk_or_orders(tmp_path):
    journal = Journal(tmp_path / "state.db")
    journal.set("intraday_state", '{"version": 2}')
    with pytest.raises(ValueError):
        IntradayState.load(journal)
    journal.close()


def test_five_minute_crossover_ignores_unfinished_bars_and_checks_gaps():
    pytest.importorskip("exchange_calendars")
    from agent_quant_system.trading.intraday import crossover, session_bounds

    now = datetime(2026, 10, 27, 16, 20, tzinfo=UTC)
    start = datetime(2026, 10, 27, 14, 30, tzinfo=UTC)
    bars = [(start + timedelta(minutes=5 * index), Decimal("100")) for index in range(22)]
    bars[-1] = (bars[-1][0], Decimal("102"))
    assert crossover(bars, now) == (now, "BUY")
    assert crossover([*bars, (now, Decimal("1"))], now) == (now, "BUY")
    with pytest.raises(ValueError):
        crossover(bars[:-2] + bars[-1:], now)
    early_close = session_bounds(datetime(2026, 11, 27, 15, tzinfo=UTC))
    assert early_close[1].hour == 18


class IntradayBroker:
    connection_generation = 1

    def __init__(self):
        self.now = datetime(2026, 10, 27, 16, 20, tzinfo=UTC)
        self.sent = []
        self.updates = {}
        self.cancelled = []
        self.cash = Decimal("10000")
        self.quantity = Decimal("0")
        self.loss = Decimal("0")
        self.fail_submit = False
        self.positions_mismatch = False
        start = self.now - timedelta(minutes=110)
        self.bars = [(start + timedelta(minutes=5 * index), Decimal("100")) for index in range(22)]
        self.bars[-1] = (self.bars[-1][0], Decimal("102"))

    def connect(self):
        pass

    def reconcile(self):
        return self.updates, bool(self.updates)

    def managed_open_orders(self, references):
        if any(
            reference not in references
            for reference, update in self.updates.items()
            if update.status not in TERMINAL
        ):
            raise RuntimeError("foreign order")
        return []

    def snapshot(self, symbols):
        positions = {}
        quantity = self.quantity + int(self.positions_mismatch)
        if quantity:
            positions["SPY"] = Position(ticker="SPY", quantity=quantity, market_price=100)
        return BrokerSnapshot(
            PortfolioSnapshot(
                cash=self.cash,
                equity=self.cash + quantity * 100,
                positions=positions,
                daily_pnl_fraction=self.loss,
            ),
            self.cash,
            {"SPY": Quote(ticker="SPY", bid="99.99", ask="100.01", observed_at=self.now)},
            self.now,
        )

    def intraday_history(self, ticker):
        return self.bars

    def submit_managed(self, order, references):
        self.sent.append(order)
        if self.fail_submit:
            raise ConnectionError("lost acknowledgement")
        self.updates[order.client_order_id] = OrderUpdate(order.client_order_id, "Submitted")

    def cancel_managed(self, reference, references):
        self.cancelled.append(reference)
        update = self.updates[reference]
        self.updates[reference] = OrderUpdate(
            reference, "Cancelled", update.filled_quantity, update.average_price
        )

    def fill(self, order, cumulative, status="Filled"):
        cumulative = Decimal(cumulative)
        delta = cumulative - self.updates[order.client_order_id].filled_quantity
        signed = delta if order.action.value == "BUY" else -delta
        self.quantity += signed
        self.cash -= signed * 100
        self.updates[order.client_order_id] = OrderUpdate(
            order.client_order_id, status, cumulative, Decimal("100")
        )


@pytest.fixture
def runtime(tmp_path):
    pytest.importorskip("exchange_calendars")
    settings = Settings(
        _env_file=None,
        strategy_mode="intraday",
        intraday_symbols="SPY",
        ib_account_id="DU12345",
        paper_trading_enabled=True,
        monitor_state_path=tmp_path / "state.db",
        monitor_stop_path=tmp_path / "STOP",
    )
    journal = Journal(settings.monitor_state_path)
    broker = IntradayBroker()
    yield settings, journal, broker
    journal.close()


def test_intraday_default_observation_and_managed_entry(runtime):
    settings, journal, broker = runtime
    assert IntradayMonitor(settings, broker, journal).cycle(broker.now) == "MONITORED"
    assert not broker.sent and not journal.orders()
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    assert runner.cycle(broker.now) == "SUBMITTED"
    assert broker.sent[0].role == "entry"
    assert broker.sent[0].quantity == 9
    assert runner.cycle(broker.now) == "MONITORED"
    assert len(broker.sent) == 1


def test_partial_fill_restart_and_close_exit_barrier(runtime):
    settings, journal, broker = runtime
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.cycle(broker.now)
    broker.fill(broker.sent[0], "4", "Submitted")
    runner.cycle(broker.now)
    assert [order.role for order in broker.sent] == ["entry", "stop"]
    restarted = IntradayMonitor(settings, broker, journal, execute=True)
    restarted.cycle(broker.now)
    assert len(broker.sent) == 2
    broker.fill(broker.sent[0], "6", "Submitted")
    restarted.cycle(broker.now)
    assert broker.sent[-1].role == "stop" and broker.sent[-1].quantity == 2
    broker.now = broker.now.replace(hour=19, minute=56)
    assert restarted.cycle(broker.now) == "RISK_HALTED"
    assert not any(order.role == "exit" for order in broker.sent)
    assert len(broker.cancelled) == 3
    restarted.cycle(broker.now)
    assert broker.sent[-1].role == "exit" and broker.sent[-1].quantity == 6
    broker.fill(broker.sent[-1], "6")
    restarted.cycle(broker.now)
    assert broker.quantity == 0
    assert not journal.active()


def test_lost_submission_remains_blocked_across_restart(runtime):
    settings, journal, broker = runtime
    broker.fail_submit = True
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    with pytest.raises(ConnectionError):
        runner.cycle(broker.now)
    restarted = IntradayMonitor(settings, broker, journal, execute=True)
    assert restarted.cycle(broker.now) == "BLOCKED"
    assert len(broker.sent) == 1


def test_daily_loss_latch_survives_restart(runtime):
    settings, journal, broker = runtime
    broker.loss = Decimal("-0.02")
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    assert runner.cycle(broker.now) == "RISK_HALTED"
    broker.loss = Decimal("0")
    restarted = IntradayMonitor(settings, broker, journal, execute=True)
    assert restarted.cycle(broker.now) == "RISK_HALTED"
    assert not broker.sent


def test_manual_stop_preserves_broker_protection(runtime):
    settings, journal, broker = runtime
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.cycle(broker.now)
    broker.fill(broker.sent[0], "4", "Submitted")
    runner.cycle(broker.now)
    settings.monitor_stop_path.touch()
    assert runner.cycle(broker.now) == "HALTED"
    assert broker.cancelled == [broker.sent[0].client_order_id]
    assert broker.updates[broker.sent[1].client_order_id].status == "Submitted"


def test_foreign_position_is_not_adopted(runtime):
    settings, journal, broker = runtime
    broker.positions_mismatch = True
    with pytest.raises(ValueError, match="dedicated account"):
        IntradayMonitor(settings, broker, journal, execute=True).cycle(broker.now)
    assert not broker.sent


def test_cli_intraday_once_is_read_only_and_modes_are_isolated(runtime, monkeypatch, tmp_path):
    pytest.importorskip("ib_async")
    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.trading import cli as trading_cli
    from agent_quant_system.trading import ibkr

    settings, journal, broker = runtime
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(trading_cli, "Settings", lambda: settings)
    monkeypatch.setattr(broker, "disconnect", lambda: None, raising=False)
    monkeypatch.setattr(ibkr, "IBKRPaperBroker", lambda settings, execute: broker)
    original = IntradayMonitor.cycle
    monkeypatch.setattr(IntradayMonitor, "cycle", lambda self: original(self, broker.now))
    client = CliRunner()
    result = client.invoke(cli.app, ["monitor", "--mode", "intraday", "--once"])
    assert result.exit_code == 0, result.output
    assert not broker.sent
    assert journal.get("strategy_mode") == "intraday"
    result = client.invoke(cli.app, ["monitor", "--mode", "intraday", "--once", "--execute"])
    assert result.exit_code != 0 and "continuous" in result.output
    result = client.invoke(cli.app, ["monitor", "--mode", "daily", "--once"])
    assert result.exit_code != 0


def test_notification_failure_blocks_only_new_entries(runtime):
    settings, journal, broker = runtime
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.entries_allowed = False
    runner.cycle(broker.now)
    assert not broker.sent
    runner.state.last_bars.clear()
    runner.entries_allowed = True
    runner.cycle(broker.now)
    broker.fill(broker.sent[0], "4", "Submitted")
    runner.entries_allowed = False
    runner.cycle(broker.now)
    assert broker.sent[-1].role == "stop"


def test_explicit_flatten_works_through_stop_latch_and_requires_confirmed_cancels(runtime):
    settings, journal, broker = runtime
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.cycle(broker.now)
    broker.fill(broker.sent[0], "9")
    runner.cycle(broker.now)
    settings.monitor_stop_path.touch()
    journal.set("halted", "operator_flatten")
    journal.set("flatten_requested", "true")
    assert runner.cycle(broker.now) == "RISK_HALTED"
    assert not any(order.role == "exit" for order in broker.sent)
    assert runner.cycle(broker.now) == "RISK_HALTED"
    assert broker.sent[-1].role == "exit"
    broker.fill(broker.sent[-1], "9")
    assert runner.cycle(broker.now) == "HALTED"
    assert broker.quantity == 0
    assert not journal.get("flatten_requested")
    assert journal.get("halted") == "operator_flatten_complete"


def test_state_journal_conflict_blocks_restore(runtime):
    settings, journal, broker = runtime
    IntradayMonitor(settings, broker, journal, execute=True).cycle(broker.now)
    journal.db.execute("UPDATE orders SET status='Filled'")
    with pytest.raises(ValueError, match="conflicts"):
        IntradayState.load(journal)


def test_cli_flatten_requires_explicit_paper_confirmation(runtime, monkeypatch):
    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.trading import cli as trading_cli

    settings, journal, broker = runtime
    IntradayMonitor(settings, broker, journal)
    monkeypatch.setattr(trading_cli, "Settings", lambda: settings)
    client = CliRunner()
    assert client.invoke(cli.app, ["monitor-flatten"]).exit_code != 0
    assert client.invoke(cli.app, ["monitor-flatten", "--confirm-paper-exit"]).exit_code == 0
    assert journal.get("flatten_requested") == "true"
    assert not broker.sent


def test_read_only_cannot_acknowledge_flatten(runtime):
    settings, journal, broker = runtime
    journal.set("flatten_requested", "true")
    assert IntradayMonitor(settings, broker, journal).cycle(broker.now) == "BLOCKED"
    assert journal.get("flatten_requested") == "true"


def test_downward_crossover_exits_after_entry_window_closes(runtime):
    settings, journal, broker = runtime
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.cycle(broker.now)
    broker.fill(broker.sent[0], "9")
    runner.cycle(broker.now)
    broker.now = broker.now.replace(hour=19, minute=50)
    start = broker.now - timedelta(minutes=110)
    broker.bars = [(start + timedelta(minutes=5 * index), Decimal("100")) for index in range(22)]
    broker.bars[-2] = (broker.bars[-2][0], Decimal("102"))
    broker.bars[-1] = (broker.bars[-1][0], Decimal("95"))
    assert runner.cycle(broker.now) == "OUTSIDE_ENTRY_WINDOW"
    assert broker.cancelled == [broker.sent[1].client_order_id]
    runner.cycle(broker.now)
    assert broker.sent[-1].role == "exit"


def test_rejected_protection_exits_but_rejected_exit_is_not_reissued(runtime):
    settings, journal, broker = runtime
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.cycle(broker.now)
    broker.fill(broker.sent[0], "9")
    runner.cycle(broker.now)
    stop = broker.sent[-1]
    broker.updates[stop.client_order_id] = OrderUpdate(stop.client_order_id, "Inactive")
    assert runner.cycle(broker.now) == "RISK_HALTED"
    exit_order = broker.sent[-1]
    assert exit_order.role == "exit"
    broker.updates[exit_order.client_order_id] = OrderUpdate(exit_order.client_order_id, "Inactive")
    runner.cycle(broker.now)
    runner.cycle(broker.now)
    assert len(broker.sent) == 3


def test_linux_repeated_polling_restart_and_trade_export(runtime, tmp_path):
    import csv
    import json
    import os
    import sqlite3
    import time
    from pathlib import Path

    settings, journal, broker = runtime
    started = time.monotonic()
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    fills = []

    def fill_and_record(order, cumulative, status="Filled"):
        previous = broker.updates[order.client_order_id].filled_quantity
        broker.fill(order, cumulative, status)
        increment = Decimal(cumulative) - previous
        fills.append({
            "data_kind": "SYNTHETIC_TEST_ONLY", "time_utc": broker.now.isoformat(),
            "reference": order.client_order_id, "ticker": order.ticker,
            "action": order.action.value, "role": order.role,
            "fill_quantity": str(increment), "cumulative_quantity": cumulative,
            "price_usd": "100", "notional_usd": str(increment * 100),
            "cash_after_usd": str(broker.cash), "position_after": str(broker.quantity),
        })

    assert runner.cycle(broker.now) == "SUBMITTED"
    fill_and_record(broker.sent[0], "4", "Submitted")
    runner.cycle(broker.now)
    for cycle in range(1000):
        broker.now += timedelta(milliseconds=10)
        if cycle % 100 == 0:
            runner = IntradayMonitor(settings, broker, journal, execute=True)
        runner.cycle(broker.now)
        assert len(broker.sent) == 2
        assert runner.state.position("SPY") == 4
    fill_and_record(broker.sent[0], "6", "Submitted")
    runner.cycle(broker.now)
    assert broker.sent[-1].role == "stop" and broker.sent[-1].quantity == 2
    broker.now = broker.now.replace(hour=19, minute=56, second=0, microsecond=0)
    runner.cycle(broker.now)
    assert not any(order.role == "exit" for order in broker.sent)
    runner = IntradayMonitor(settings, broker, journal, execute=True)
    runner.cycle(broker.now)
    assert broker.sent[-1].role == "exit" and broker.sent[-1].quantity == 6
    fill_and_record(broker.sent[-1], "6")
    runner.cycle(broker.now)
    assert broker.quantity == 0 and broker.cash == Decimal("10000")
    assert not journal.active()
    assert journal.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    IntradayState.load(journal)

    output = (
        Path(os.environ.get("AQS_VALIDATION_ARTIFACT_DIR", str(tmp_path))) / "intraday-synthetic"
    )
    output.mkdir(parents=True, exist_ok=False)
    with (output / "trades.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fills[0]))
        writer.writeheader()
        writer.writerows(fills)
    rows = journal.orders()
    orders = []
    for row in rows:
        proposal = json.loads(row["payload"])
        orders.append({"data_kind": "SYNTHETIC_TEST_ONLY", "reference": row["reference"],
                       "ticker": proposal["ticker"], "role": proposal["role"],
                       "action": proposal["action"], "quantity": proposal["quantity"],
                       "status": row["status"], "filled": row["filled"],
                       "average_price": row["average_price"]})
    with (output / "orders.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(orders[0]))
        writer.writeheader()
        writer.writerows(orders)
    with sqlite3.connect(output / "audit.sqlite3") as backup:
        journal.db.backup(backup)
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    metadata = {
        "status": "PASSED", "data_kind": "SYNTHETIC_TEST_ONLY", "broker_connected": False,
        "market_backtest": False, "polling_cycles": 1000, "state_reloads": 11,
        "fill_records": len(fills), "order_intents": len(rows), "final_position": "0",
        "final_cash_usd": str(broker.cash), "wall_time_seconds": time.monotonic() - started,
        "limitations": ["Accelerated synthetic clock, not a multi-day soak test",
                        "Controlled broker substitute, no IBKR connectivity",
                        "Fixed fill price; fees and market liquidity are not modeled",
                        "No profitability inference"],
    }
    (output / "summary.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    with (output / "trades.csv").open(newline="", encoding="utf-8") as stream:
        persisted = list(csv.DictReader(stream))
    assert sum(Decimal(row["fill_quantity"]) * (1 if row["action"] == "BUY" else -1)
               for row in persisted) == 0
