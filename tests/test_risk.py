from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent_quant_system.models import (
    AgentSignal,
    PortfolioSnapshot,
    Position,
    RiskLimits,
    TradeAction,
)
from agent_quant_system.risk import RiskEngine


def portfolio(*, daily_pnl: str = "0") -> PortfolioSnapshot:
    return PortfolioSnapshot(
        cash=Decimal("10000"),
        equity=Decimal("10000"),
        daily_pnl_fraction=Decimal(daily_pnl),
    )


def signal(*, confidence: str = "0.80", target_weight: str = "0.05") -> AgentSignal:
    return AgentSignal(
        ticker="AAPL",
        action=TradeAction.BUY,
        confidence=Decimal(confidence),
        target_weight=Decimal(target_weight),
        thesis="Test signal",
    )


def test_approves_bounded_buy() -> None:
    decision = RiskEngine().evaluate(signal(), portfolio(), Decimal("200"))

    assert decision.approved is True
    assert decision.order is not None
    assert decision.order.quantity == Decimal("2.500000")
    assert decision.order.estimated_notional == Decimal("500.000000")


def test_rejects_low_confidence_signal() -> None:
    decision = RiskEngine().evaluate(signal(confidence="0.40"), portfolio(), Decimal("200"))

    assert decision.approved is False
    assert "confidence_below_threshold" in decision.reasons


def test_rejects_oversized_target_instead_of_silently_clamping() -> None:
    limits = RiskLimits(max_position_weight=Decimal("0.10"))
    decision = RiskEngine(limits).evaluate(
        signal(target_weight="0.25"),
        portfolio(),
        Decimal("200"),
    )

    assert decision.approved is False
    assert "max_position_weight_exceeded" in decision.reasons


def test_daily_loss_limit_is_a_kill_switch() -> None:
    decision = RiskEngine().evaluate(signal(), portfolio(daily_pnl="-0.03"), Decimal("200"))

    assert decision.approved is False
    assert "daily_loss_limit_reached" in decision.reasons


def test_price_change_cannot_cause_overselling() -> None:
    book = PortfolioSnapshot(
        cash=Decimal("8000"),
        equity=Decimal("10000"),
        positions={"AAPL": Position(ticker="AAPL", quantity=10, market_price=200)},
    )
    sell = signal().model_copy(update={"action": TradeAction.SELL, "target_weight": Decimal("0")})
    decision = RiskEngine().evaluate(sell, book, Decimal("100"))
    assert decision.approved
    assert decision.order.quantity == Decimal("10")


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_invalid_reference_price_is_rejected(price: str) -> None:
    decision = RiskEngine().evaluate(signal(), portfolio(), Decimal(price))
    assert not decision.approved
    assert decision.reasons == ("reference_price_must_be_finite_and_positive",)


@pytest.mark.parametrize("offset", [-301, 1])
def test_stale_and_future_signals_are_rejected(offset: int) -> None:
    now = datetime.now(UTC)
    proposal = signal().model_copy(update={"generated_at": now + timedelta(seconds=offset)})
    assert not RiskEngine().evaluate(proposal, portfolio(), Decimal("200"), now=now).approved


def test_reducing_overweight_position_is_allowed_after_loss() -> None:
    book = PortfolioSnapshot(
        cash=7000,
        equity=10000,
        daily_pnl_fraction="-0.03",
        positions={"AAPL": Position(ticker="AAPL", quantity=15, market_price=200)},
    )
    proposal = signal().model_copy(
        update={"action": TradeAction.SELL, "target_weight": Decimal("0.15")},
    )
    assert RiskEngine().evaluate(proposal, book, Decimal("200")).approved


def test_account_equity_and_position_immutability() -> None:
    with pytest.raises(ValueError, match="reconcile"):
        PortfolioSnapshot(cash=10000, equity=1000000)
    book = portfolio()
    with pytest.raises(TypeError):
        book.positions["AAPL"] = Position(ticker="AAPL", quantity=1, market_price=200)
    assert PortfolioSnapshot.model_validate_json(book.model_dump_json()) == book
