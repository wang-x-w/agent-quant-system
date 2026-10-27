from datetime import UTC, datetime

import pytest

from agent_quant_system.research.backtest import completed_sessions, simulate, validate_prices

pytest.importorskip("backtrader")
pd = pytest.importorskip("pandas")
pytest.importorskip("exchange_calendars")


def prices_fixture():
    sessions = completed_sessions(datetime(2026, 10, 22, 7, tzinfo=UTC))
    values = [100.0 + offset for offset in range(len(sessions))]
    frame = pd.DataFrame({
        "Open": values, "Close": values,
        "High": [value + 2 for value in values],
        "Low": [value - 2 for value in values], "Volume": 1000000,
    }, index=sessions)
    return {"AAPL": frame.copy(), "SPY": frame.copy()}, sessions


def test_window_excludes_incomplete_session():
    _, sessions = prices_fixture()
    assert sessions[-5].date().isoformat() == "2026-10-15"
    assert sessions[-1].date().isoformat() == "2026-10-21"


def test_broker_updates_cash_and_accounts_for_fills():
    prices, sessions = prices_fixture()
    curve, strategy = simulate(prices, sessions)
    assert len(curve) == 6
    assert strategy.fills
    assert strategy.failures == []
    assert (curve["cash"] >= 0).all()
    assert (curve["shares_AAPL"] >= 0).all()
    assert curve["cash"].iloc[-1] < 10000
    total_cost = sum(fill["notional"] + fill["commission"] for fill in strategy.fills)
    assert curve["cash"].iloc[-1] == pytest.approx(10000 - total_cost)
    for fill in strategy.fills:
        assert fill["date"] > fill["signal_date"]
        assert fill["date"] >= sessions[-5].date().isoformat()
    final_value = curve["cash"].iloc[-1] + curve["shares_AAPL"].iloc[-1] * 124
    assert curve["equity"].iloc[-1] == pytest.approx(final_value)


def test_future_close_does_not_change_orders():
    prices, sessions = prices_fixture()
    _, original = simulate(prices, sessions)
    prices["AAPL"].loc[sessions[-1], ["Close", "High"]] = [200.0, 201.0]
    _, altered = simulate(prices, sessions)
    assert original.fills == altered.fills
    assert original.decisions == altered.decisions


def test_missing_and_invalid_bars_are_rejected():
    prices, sessions = prices_fixture()
    with pytest.raises(ValueError, match="Missing or duplicate"):
        validate_prices(prices["AAPL"].iloc[:-1], sessions)
    prices["AAPL"].loc[sessions[-1], "Close"] = float("nan")
    with pytest.raises(ValueError, match="finite and positive"):
        validate_prices(prices["AAPL"], sessions)


def test_sell_reduces_position_and_adds_cash():
    prices, sessions = prices_fixture()
    prices["AAPL"].loc[sessions[-3], ["Open", "Close", "Low", "High"]] = [120, 90, 89, 121]
    curve, strategy = simulate(prices, sessions)
    sells = [fill for fill in strategy.fills if fill["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["quantity"] == strategy.fills[0]["quantity"]
    sell_day = curve.loc[curve["date"] == sells[0]["date"]].iloc[0]
    assert sell_day["shares_AAPL"] == 0
    cash_flows = sum(
        fill["notional"] * (-1 if fill["action"] == "BUY" else 1) - fill["commission"]
        for fill in strategy.fills
    )
    assert curve["cash"].iloc[-1] == pytest.approx(10000 + cash_flows)


def test_exported_backtest_trades_reconcile_with_account(tmp_path):
    import json

    from agent_quant_system.research.backtest import save_report

    prices, sessions = prices_fixture()
    curve, strategy = simulate(prices, sessions)
    save_report(tmp_path, prices, sessions, curve, strategy,
                {"source": "SYNTHETIC_TEST_ONLY", "broker_connected": False})
    trades = pd.read_csv(tmp_path / "trades.csv")
    account = pd.read_csv(tmp_path / "equity.csv")
    summary = json.loads((tmp_path / "summary.json").read_text())
    cash_flow = sum(
        row.notional * (-1 if row.action == "BUY" else 1) - row.commission
        for row in trades.itertuples()
    )
    assert account["cash"].iloc[-1] == pytest.approx(10000 + cash_flow)
    assert summary["fills"] == len(trades)
    assert summary["final_equity"] == pytest.approx(account["equity"].iloc[-1])
    assert (tmp_path / "curves.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_research_and_legacy_backtest_entrypoints():
    import subprocess
    import sys

    from agent_quant_system import backtest as legacy
    from agent_quant_system.research import backtest

    assert legacy.simulate is backtest.simulate
    assert legacy.main is backtest.main
    for module in ("agent_quant_system.backtest", "agent_quant_system.research.backtest"):
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "--data-dir" in result.stdout