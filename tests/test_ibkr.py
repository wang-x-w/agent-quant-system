from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

pytest.importorskip("ib_async")

from agent_quant_system.config import Settings
from agent_quant_system.models import OrderProposal, PortfolioSnapshot
from agent_quant_system.trading.ibkr import IBKRPaperBroker, amount
from agent_quant_system.trading.monitor import BrokerSnapshot, Quote


def broker(*, execute=False, accounts=None):
    client = Mock()
    client.pnlEvent = MagicMock()
    client.managedAccounts.return_value = accounts or ["DU12345"]
    client.reqAllOpenOrders.return_value = []
    client.reqCompletedOrders.return_value = []
    client.reqExecutions.return_value = []
    client.isConnected.return_value = True
    client.qualifyContracts.return_value = [SimpleNamespace(secType="STK", currency="USD")]
    config = Settings(_env_file=None, ib_account_id="DU12345", paper_trading_enabled=True)
    return IBKRPaperBroker(config, execute=execute, client=client), client


def proposal():
    return OrderProposal(
        client_order_id="aqs-test",
        ticker="AAPL",
        action="BUY",
        quantity=1,
        reference_price=100,
        rationale="test",
    )


def test_real_account_is_rejected_before_order_submission():
    gateway, client = broker(execute=True, accounts=["U12345", "DU12345"])
    with pytest.raises(RuntimeError, match="paper-only"):
        gateway.submit(proposal())
    client.placeOrder.assert_not_called()


def test_read_only_and_limit_order_contract(monkeypatch, tmp_path):
    gateway, client = broker()
    with pytest.raises(RuntimeError, match="read-only"):
        gateway.submit(proposal())
    client.placeOrder.assert_not_called()
    gateway, client = broker(execute=True)
    gateway.settings.monitor_stop_path = tmp_path / "STOP"
    now = datetime.now(UTC)
    gateway.last_snapshot = BrokerSnapshot(
        PortfolioSnapshot(cash=10000, equity=10000),
        Decimal("10000"),
        {"AAPL": Quote(ticker="AAPL", bid="99.99", ask="100.01", observed_at=now)},
        now,
    )
    monkeypatch.setattr(
        "agent_quant_system.trading.ibkr.market_session", lambda now: (True, now.date())
    )
    gateway.submit(proposal())
    submitted = client.placeOrder.call_args.args[1]
    assert submitted.orderType == "LMT"
    assert submitted.account == "DU12345"
    assert submitted.orderRef == "aqs-test"
    assert submitted.outsideRth is False
    assert submitted.tif == "DAY"
    gateway.settings.monitor_stop_path.touch()
    with pytest.raises(RuntimeError, match="stop requested"):
        gateway.submit(proposal())
    assert client.placeOrder.call_count == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1.7976931348623157e308"])
def test_broker_unset_numbers_fail_closed(value):
    with pytest.raises(ValueError, match="unavailable"):
        amount(value)


def snapshot_client():
    gateway, client = broker()
    contract = SimpleNamespace(secType="STK", currency="USD", symbol="AAPL", conId=1)
    client.qualifyContracts.return_value = [contract]
    client.reqPositions.return_value = [
        SimpleNamespace(
            account="DU12345",
            contract=contract,
            position=10,
            avgCost=90,
        )
    ]
    now = datetime.now(UTC)
    client.reqTickers.return_value = [
        SimpleNamespace(
            contract=contract,
            time=now,
            bid=99.99,
            ask=100.01,
            minTick=0.01,
            marketDataType=1,
        )
    ]
    client.accountValues.return_value = [
        SimpleNamespace(tag=tag, value=value, currency="USD")
        for tag, value in {
            "Currency": "USD",
            "TotalCashValue": "9000",
            "NetLiquidation": "10000",
            "AvailableFunds": "9000",
            "SettledCash": "8000",
        }.items()
    ]
    gateway.pnl = SimpleNamespace(dailyPnL=-100)
    gateway.pnl_updated_at = now
    return gateway, client


def test_full_account_snapshot_and_settled_funds():
    gateway, client = snapshot_client()
    snapshot = gateway.snapshot(("AAPL",))
    assert snapshot.portfolio.equity == 10000
    assert snapshot.portfolio.positions["AAPL"].quantity == 10
    assert snapshot.portfolio.positions["AAPL"].average_cost == 90
    assert snapshot.available_funds == 8000
    assert snapshot.portfolio.daily_pnl_fraction == Decimal("-100") / Decimal("10100")
    client.reqAccountUpdates.assert_called_once_with("DU12345")


@pytest.mark.parametrize("condition", ["currency", "derivative", "pnl", "delayed", "foreign_cash"])
def test_unsupported_account_or_market_data_is_blocked(condition):
    gateway, client = snapshot_client()
    if condition == "currency":
        client.accountValues.return_value[0].value = "EUR"
    elif condition == "derivative":
        client.reqPositions.return_value[0].contract.secType = "OPT"
    elif condition == "pnl":
        gateway.pnl_updated_at -= timedelta(seconds=31)
    elif condition == "delayed":
        client.reqTickers.return_value[0].marketDataType = 3
    else:
        client.accountValues.return_value.append(
            SimpleNamespace(tag="CashBalance", value="100", currency="EUR"),
        )
    with pytest.raises(ValueError):
        gateway.snapshot(("AAPL",))
    client.placeOrder.assert_not_called()


def test_execution_ids_are_deduplicated_in_broker_reconciliation():
    gateway, client = broker()
    trade = SimpleNamespace(
        order=SimpleNamespace(account="DU12345", orderRef="aqs-test"),
        orderStatus=SimpleNamespace(filled=0, avgFillPrice=0, status="Filled"),
    )
    client.reqCompletedOrders.return_value = [trade]
    execution = SimpleNamespace(
        acctNumber="DU12345", orderRef="aqs-test", execId="exec-1", shares=1, price=100
    )
    client.reqExecutions.return_value = [SimpleNamespace(execution=execution)] * 2
    updates, opened = gateway.reconcile()
    assert not opened
    assert updates["aqs-test"].filled_quantity == 1
    assert updates["aqs-test"].executions == ("exec-1",)


def test_managed_protection_and_exit_cannot_oversell(monkeypatch, tmp_path):
    gateway, client = broker(execute=True)
    gateway.settings.monitor_stop_path = tmp_path / "STOP"
    contract = SimpleNamespace(secType="STK", currency="USD", symbol="AAPL")
    client.reqPositions.return_value = [
        SimpleNamespace(
            account="DU12345",
            contract=contract,
            position=4,
        )
    ]
    monkeypatch.setattr(
        "agent_quant_system.trading.ibkr.market_session", lambda now: (True, now.date())
    )
    stop = OrderProposal(
        client_order_id="aqs-stop",
        ticker="AAPL",
        action="SELL",
        quantity=4,
        reference_price=99,
        role="stop",
        order_type="STP",
        rationale="test",
    )
    gateway.submit_managed(stop, {"aqs-stop"})
    assert client.placeOrder.call_args.args[1].orderType == "STP"
    trade = SimpleNamespace(
        order=SimpleNamespace(
            account="DU12345", orderRef="aqs-stop", clientId=71, action="SELL", totalQuantity=4
        ),
        orderStatus=SimpleNamespace(filled=0),
        contract=contract,
        isDone=lambda: False,
    )
    client.reqAllOpenOrders.return_value = [trade]
    exit_order = OrderProposal(
        client_order_id="aqs-exit",
        ticker="AAPL",
        action="SELL",
        quantity=4,
        reference_price=100,
        role="exit",
        order_type="MKT",
        rationale="test",
    )
    with pytest.raises(ValueError, match="exceed actual"):
        gateway.submit_managed(exit_order, {"aqs-stop", "aqs-exit"})
    gateway.cancel_managed("aqs-stop", {"aqs-stop", "aqs-exit"})
    client.cancelOrder.assert_called_once_with(trade.order)
    client.reqAllOpenOrders.return_value = []
    gateway.submit_managed(exit_order, {"aqs-stop", "aqs-exit"})
    assert client.placeOrder.call_args.args[1].orderType == "MKT"
    client.reqPositions.return_value[0].position = 3
    with pytest.raises(ValueError, match="exceed actual"):
        gateway.submit_managed(exit_order, {"aqs-stop", "aqs-exit"})


def test_managed_adapter_rejects_foreign_orders():
    gateway, client = broker(execute=True)
    client.reqAllOpenOrders.return_value = [
        SimpleNamespace(
            order=SimpleNamespace(account="DU12345", orderRef="manual", clientId=9),
            isDone=lambda: False,
        )
    ]
    with pytest.raises(RuntimeError, match="foreign"):
        gateway.managed_open_orders({"aqs-known"})
    client.placeOrder.assert_not_called()
