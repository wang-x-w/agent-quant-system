from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent_quant_system.config import Settings
from agent_quant_system.models import OrderProposal, PortfolioSnapshot
from agent_quant_system.trading.monitor import (
    BrokerSnapshot,
    Journal,
    Monitor,
    OrderUpdate,
    Quote,
    deliver_notifications,
    monitor_lock,
)


def settings(tmp_path, **values):
    return Settings(
        _env_file=None,
        ib_account_id="DU12345",
        monitor_symbols="AAPL",
        monitor_state_path=tmp_path / "state.db",
        monitor_stop_path=tmp_path / "STOP",
        **values,
    )


def order():
    return OrderProposal(
        client_order_id="aqs-test",
        ticker="AAPL",
        action="BUY",
        quantity=1,
        reference_price=100,
        rationale="test",
    )


def test_durable_intent_deduplicates_and_unknown_blocks_restart(tmp_path):
    path = tmp_path / "state.db"
    first = Journal(path)
    assert first.claim("signal-1", order())
    first.close()
    second = Journal(path)
    assert not second.claim("signal-1", order())
    second.reconcile({})
    assert second.active()[0]["status"] == "UNKNOWN"
    second.reconcile({"aqs-test": OrderUpdate("aqs-test", "Filled", Decimal("1"), Decimal("100"))})
    assert not second.active()
    assert not second.claim("signal-1", order())
    second.close()


def test_paper_guards_and_quote_freshness(tmp_path):
    config = settings(tmp_path)
    config.guard_monitor(execute=False)
    with pytest.raises(ValueError, match="PAPER_TRADING_ENABLED"):
        config.guard_monitor(execute=True)
    with pytest.raises(ValueError, match="DU-prefixed"):
        config.model_copy(update={"ib_account_id": "U12345"}).guard_monitor(execute=False)
    with pytest.raises(ValueError, match="paper ports"):
        config.model_copy(update={"ib_gateway_port": 4001}).guard_monitor(execute=False)
    now = datetime.now(UTC)
    quote = Quote(ticker="AAPL", bid="99.99", ask="100.01", observed_at=now)
    quote.check(now, config)
    for invalid in [
        quote.model_copy(update={"market_data_type": 3}),
        quote.model_copy(update={"observed_at": now - timedelta(seconds=31)}),
    ]:
        with pytest.raises(ValueError, match="real-time"):
            invalid.check(now, config)


def test_only_one_monitor_can_own_a_database(tmp_path):
    with (
        monitor_lock(tmp_path / "state.db"),
        pytest.raises(RuntimeError, match="another monitor"),
        monitor_lock(tmp_path / "state.db"),
    ):
        pass


def test_database_cannot_be_reused_for_another_account(tmp_path):
    journal = Journal(tmp_path / "state.db")
    journal.bind_account("DU12345")
    with pytest.raises(ValueError, match="different account"):
        journal.bind_account("DU98765")
    journal.close()


def test_linux_process_lock_and_durable_database(tmp_path):
    import subprocess
    import sys

    if sys.platform != "linux":
        pytest.skip("Linux runtime validation")
    path = tmp_path / "process-state.db"
    journal = Journal(path)
    journal.set("probe", "durable")
    journal.close()
    script = """
import sys
from pathlib import Path
from agent_quant_system.trading.monitor import Journal, monitor_lock
with monitor_lock(Path(sys.argv[1])):
    journal = Journal(Path(sys.argv[1]))
    assert journal.get("probe") == "durable"
    journal.set("child", "verified")
    journal.close()
"""
    with monitor_lock(path):
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10,
        )
        assert blocked.returncode != 0
        assert "another monitor" in blocked.stderr
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    restored = Journal(path)
    assert restored.get("child") == "verified"
    assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored.close()


NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)


class FakeBroker:
    def __init__(self):
        import exchange_calendars as xcals

        dates = xcals.get_calendar("XNYS").sessions_window("2026-10-26", -19)
        self.bars = [(stamp.date(), Decimal(100 + index)) for index, stamp in enumerate(dates)]
        self.quote = Quote(ticker="AAPL", bid="99.99", ask="100.01", observed_at=NOW)
        self.sent = []
        self.updates = {}
        self.external_open = False
        self.fail_submit = False
        self.cancelled = False

    def history(self, ticker):
        return self.bars

    def snapshot(self, symbols):
        return BrokerSnapshot(
            PortfolioSnapshot(cash=10000, equity=10000), Decimal("10000"), {"AAPL": self.quote}, NOW
        )

    def reconcile(self):
        return self.updates, self.external_open

    def submit(self, order):
        self.sent.append(order)
        if self.fail_submit:
            raise ConnectionError("lost acknowledgement")
        self.updates[order.client_order_id] = OrderUpdate(
            order.client_order_id,
            "Filled",
            order.quantity,
            order.reference_price,
            ("exec-1",),
        )

    def cancel_own_orders(self):
        self.cancelled = True


@pytest.fixture
def cycle_setup(tmp_path):
    pytest.importorskip("exchange_calendars")
    config = settings(tmp_path, paper_trading_enabled=True)
    journal = Journal(config.monitor_state_path)
    broker = FakeBroker()
    yield config, journal, broker
    journal.close()


def test_monitoring_is_default_and_execution_deduplicates(cycle_setup):
    config, journal, broker = cycle_setup
    assert Monitor(config, broker, journal).cycle(NOW) == "MONITORED"
    assert not broker.sent
    assert not journal.orders()
    runner = Monitor(config, broker, journal, execute=True)
    assert runner.cycle(NOW) == "SUBMITTED"
    assert runner.cycle(NOW) == "MONITORED"
    assert len(broker.sent) == 1
    assert not journal.active()


def test_network_failure_does_not_resubmit_after_restart(cycle_setup):
    config, journal, broker = cycle_setup
    broker.fail_submit = True
    runner = Monitor(config, broker, journal, execute=True)
    with pytest.raises(ConnectionError):
        runner.cycle(NOW)
    restarted = Monitor(config, broker, journal, execute=True)
    assert restarted.cycle(NOW) == "BLOCKED"
    assert len(broker.sent) == 1
    assert journal.active()[0]["status"] == "UNKNOWN"


def test_partial_fill_blocks_until_reconciled_without_double_counting(cycle_setup):
    config, journal, broker = cycle_setup
    journal.claim("test", order())
    update = OrderUpdate("aqs-test", "Submitted", Decimal("0.5"), Decimal("100"), ("exec-1",))
    broker.updates = {"aqs-test": update}
    runner = Monitor(config, broker, journal, execute=True)
    assert runner.cycle(NOW) == "BLOCKED"
    assert runner.cycle(NOW) == "BLOCKED"
    assert Decimal(journal.active()[0]["filled"]) == Decimal("0.5")
    assert journal.active()[0]["executions"] == '["exec-1"]'
    assert not broker.sent


def test_stop_persists_and_never_opens_orders(cycle_setup):
    config, journal, broker = cycle_setup
    config.monitor_stop_path.touch()
    assert Monitor(config, broker, journal, execute=True).cycle(NOW) == "HALTED"
    config.monitor_stop_path.unlink()
    assert Monitor(config, broker, journal, execute=True).cycle(NOW) == "HALTED"
    assert journal.get("halted")
    assert broker.cancelled and not broker.sent


@pytest.mark.parametrize("condition", ["delayed", "stale", "missing_bars", "external_order"])
def test_invalid_market_or_external_orders_never_submit(cycle_setup, condition):
    config, journal, broker = cycle_setup
    runner = Monitor(config, broker, journal, execute=True)
    if condition == "external_order":
        broker.external_open = True
        assert runner.cycle(NOW) == "BLOCKED"
    else:
        if condition == "delayed":
            broker.quote = broker.quote.model_copy(update={"market_data_type": 3})
        elif condition == "stale":
            broker.quote = broker.quote.model_copy(
                update={"observed_at": NOW - timedelta(seconds=31)}
            )
        else:
            broker.bars = broker.bars[:-1]
        with pytest.raises((ValueError, KeyError)):
            runner.cycle(NOW)
    assert not broker.sent


def test_closed_market_and_notification_retry(cycle_setup, monkeypatch):
    config, journal, broker = cycle_setup
    sunday = datetime(2026, 10, 25, 15, tzinfo=UTC)
    assert Monitor(config, broker, journal, execute=True).cycle(sunday) == "CLOSED"
    assert not broker.sent
    from pydantic import SecretStr

    config.notification_webhook = SecretStr("https://example.invalid/private")

    def unavailable(*args, **kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr("agent_quant_system.trading.monitor.urlopen", unavailable)
    with pytest.raises(OSError):
        deliver_notifications(journal, config)
    assert journal.db.execute("SELECT count(*) FROM events WHERE delivered=0").fetchone()[0] == 1


def test_cli_stop_status_and_guarded_resume(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.trading import cli as trading_cli

    config = settings(tmp_path)
    monkeypatch.setattr(trading_cli, "Settings", lambda: config)
    client = CliRunner()
    assert client.invoke(cli.app, ["monitor-stop"]).exit_code == 0
    assert config.monitor_stop_path.exists()
    assert "operator_stop" in client.invoke(cli.app, ["monitor-status"]).output
    assert client.invoke(cli.app, ["monitor-resume"]).exit_code != 0
    assert client.invoke(cli.app, ["monitor-resume", "--confirm-reviewed"]).exit_code == 0
    assert not config.monitor_stop_path.exists()
    journal = Journal(config.monitor_state_path)
    journal.claim("test", order())
    journal.close()
    assert client.invoke(cli.app, ["monitor-resume", "--confirm-reviewed"]).exit_code != 0


def test_cli_rejects_execution_without_config_confirmation(tmp_path, monkeypatch):
    pytest.importorskip("ib_async")
    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.trading import cli as trading_cli

    config = settings(tmp_path)
    monkeypatch.setattr(trading_cli, "Settings", lambda: config)
    result = CliRunner().invoke(cli.app, ["monitor", "--execute", "--once"])
    assert result.exit_code != 0
    assert "PAPER_TRADING_ENABLED" in result.output


def test_cli_once_defaults_to_observation_and_requires_execute(cycle_setup, monkeypatch, tmp_path):
    pytest.importorskip("ib_async")
    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.trading import cli as trading_cli
    from agent_quant_system.trading import ibkr

    config, journal, broker = cycle_setup
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(trading_cli, "Settings", lambda: config)
    monkeypatch.setattr(broker, "connect", lambda: None, raising=False)
    monkeypatch.setattr(broker, "disconnect", lambda: None, raising=False)
    modes = []

    def factory(settings, *, execute):
        modes.append(execute)
        return broker

    original_cycle = Monitor.cycle
    monkeypatch.setattr(Monitor, "cycle", lambda self: original_cycle(self, NOW))
    monkeypatch.setattr(ibkr, "IBKRPaperBroker", factory)
    client = CliRunner()
    result = client.invoke(cli.app, ["monitor", "--once"])
    assert result.exit_code == 0, result.output
    assert "MONITORED" in result.output
    assert not broker.sent
    result = client.invoke(cli.app, ["monitor", "--execute", "--once"])
    assert result.exit_code == 0, result.output
    assert "SUBMITTED" in result.output
    assert len(broker.sent) == 1
    assert modes == [False, True]
    assert journal.orders()[0]["status"] == "Filled"


def test_cli_connection_retries_are_bounded_and_latch_stop(cycle_setup, monkeypatch, tmp_path):
    pytest.importorskip("ib_async")
    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.trading import cli as trading_cli
    from agent_quant_system.trading import ibkr

    config, journal, broker = cycle_setup
    attempts = []

    def disconnected():
        attempts.append(True)
        raise ConnectionError("unavailable")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(trading_cli, "Settings", lambda: config)
    monkeypatch.setattr(broker, "connect", disconnected, raising=False)
    monkeypatch.setattr(broker, "disconnect", lambda: None, raising=False)
    monkeypatch.setattr(broker, "wait", lambda seconds: None, raising=False)
    monkeypatch.setattr(ibkr, "IBKRPaperBroker", lambda settings, execute: broker)
    result = CliRunner().invoke(cli.app, ["monitor"])
    assert result.exit_code == 1
    assert len(attempts) == 3
    assert journal.get("halted") == "repeated_failure"
    assert not broker.sent
