from decimal import Decimal

import pytest

from agent_quant_system.models import AgentSignal, PortfolioSnapshot, TradeAction
from agent_quant_system.research.execution import PaperExecutionGateway
from agent_quant_system.research.providers import (
    StaticResearchProvider,
    TradingAgentsResearchProvider,
)
from agent_quant_system.research.service import TradingSystem
from agent_quant_system.risk import RiskEngine


def make_system(signal: AgentSignal, gateway: PaperExecutionGateway) -> TradingSystem:
    return TradingSystem(
        research=StaticResearchProvider(signal),
        risk=RiskEngine(),
        execution=gateway,
    )


def test_rejected_signal_never_reaches_execution() -> None:
    signal = AgentSignal(
        ticker="MSFT",
        action=TradeAction.BUY,
        confidence=Decimal("0.30"),
        target_weight=Decimal("0.05"),
        thesis="Low-confidence proposal",
    )
    gateway = PaperExecutionGateway()

    result = make_system(signal, gateway).run(
        "MSFT",
        PortfolioSnapshot(cash=Decimal("10000"), equity=Decimal("10000")),
        Decimal("400"),
    )

    assert result.risk.approved is False
    assert result.execution is None
    assert gateway.receipts == []


def test_approved_signal_is_paper_filled() -> None:
    signal = AgentSignal(
        ticker="MSFT",
        action=TradeAction.BUY,
        confidence=Decimal("0.90"),
        target_weight=Decimal("0.05"),
        thesis="High-confidence proposal",
    )
    gateway = PaperExecutionGateway()

    result = make_system(signal, gateway).run(
        "MSFT",
        PortfolioSnapshot(cash=Decimal("10000"), equity=Decimal("10000")),
        Decimal("400"),
    )

    assert result.risk.approved is True
    assert result.execution is not None
    assert result.execution.status == "FILLED"
    assert len(gateway.receipts) == 1


def test_ticker_mismatch_is_rejected_before_execution() -> None:
    signal = AgentSignal(
        ticker="NVDA",
        action=TradeAction.BUY,
        confidence=Decimal("0.90"),
        target_weight=Decimal("0.05"),
        thesis="Wrong instrument",
    )
    gateway = PaperExecutionGateway()

    result = make_system(signal, gateway).run(
        "AAPL",
        PortfolioSnapshot(cash=Decimal("10000"), equity=Decimal("10000")),
        Decimal("200"),
    )

    assert result.risk.reasons == ("research_ticker_mismatch",)
    assert gateway.receipts == []


def test_paper_submission_is_idempotent() -> None:
    signal = AgentSignal(
        ticker="AAPL", action="BUY", confidence="0.9", target_weight="0.05", thesis="test"
    )
    decision = RiskEngine().evaluate(
        signal,
        PortfolioSnapshot(cash=10000, equity=10000),
        Decimal("200"),
    )
    gateway = PaperExecutionGateway()
    assert gateway.submit(decision) == gateway.submit(decision)
    assert len(gateway.receipts) == 1
    conflicting = decision.model_copy(
        update={
            "order": decision.order.model_copy(update={"quantity": Decimal("1")}),
        }
    )
    with pytest.raises(ValueError, match="different order"):
        gateway.submit(conflicting)


def test_research_parser_does_not_guess_from_prose() -> None:
    with pytest.raises(ValueError, match="exact"):
        TradingAgentsResearchProvider._extract_rating("Do not BUY; final recommendation SELL")
    assert TradingAgentsResearchProvider._extract_rating("REVIEW") == "REVIEW"
    provider = TradingAgentsResearchProvider.__new__(TradingAgentsResearchProvider)
    provider.default_confidence = Decimal("0")
    assert provider._extract_confidence("BUY") == 0
    with pytest.raises(ValueError, match="confidence"):
        provider._extract_confidence({"confidence": 999})


def test_research_command_never_submits_and_keeps_report(tmp_path, monkeypatch):
    pytest.importorskip("ib_async")
    import json
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from typer.testing import CliRunner

    from agent_quant_system import cli
    from agent_quant_system.config import Settings
    from agent_quant_system.research import cli as research_cli
    from agent_quant_system.trading import ibkr
    from agent_quant_system.trading.monitor import BrokerSnapshot

    settings = Settings(_env_file=None, ib_account_id="DU12345", research_provider="tradingagents")
    broker = Mock()
    broker.snapshot.return_value = BrokerSnapshot(
        PortfolioSnapshot(cash=10000, equity=10000),
        Decimal("10000"),
        {},
        datetime.now(UTC),
    )
    factory = Mock(return_value=broker)
    provider = Mock()
    provider.propose.return_value = AgentSignal(
        ticker="SPY", action="HOLD", confidence=0, target_weight=0, thesis="advisory report"
    )
    monkeypatch.setattr(research_cli, "Settings", lambda: settings)
    monkeypatch.setattr(research_cli, "TradingAgentsResearchProvider", lambda **kwargs: provider)
    monkeypatch.setattr(ibkr, "IBKRPaperBroker", factory)
    output = tmp_path / "research.json"
    result = CliRunner().invoke(cli.app, ["research", "--ticker", "SPY", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text())["advisory_only"] is True
    assert factory.call_args.kwargs["execute"] is False
    broker.submit.assert_not_called()
    broker.submit_managed.assert_not_called()
    broker.disconnect.assert_called_once()
    assert (
        CliRunner()
        .invoke(cli.app, ["research", "--ticker", "SPY", "--output", str(output)])
        .exit_code
        != 0
    )
