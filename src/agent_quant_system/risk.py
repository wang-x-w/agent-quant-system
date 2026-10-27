from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from agent_quant_system.models import (
    AgentSignal,
    OrderProposal,
    PortfolioSnapshot,
    RiskDecision,
    RiskLimits,
    TradeAction,
)


class RiskEngine:
    """Convert an untrusted agent signal into a bounded order proposal."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        signal: AgentSignal,
        portfolio: PortfolioSnapshot,
        reference_price: Decimal,
        *,
        now: datetime | None = None,
    ) -> RiskDecision:
        reasons = self._pretrade_rejections(
            signal, portfolio, reference_price, now or datetime.now(UTC)
        )
        if reasons:
            return RiskDecision(approved=False, reasons=tuple(reasons))

        current_position = portfolio.position_for(signal.ticker)
        current_quantity = current_position.quantity if current_position else Decimal("0")
        previous_value = current_position.market_value if current_position else Decimal("0")
        current_value = current_quantity * reference_price
        marked_equity = portfolio.equity - previous_value + current_value
        if marked_equity <= 0:
            return self._reject("non_positive_marked_equity")
        target_value = marked_equity * signal.target_weight
        desired_delta = target_value - current_value

        if signal.action is TradeAction.BUY and desired_delta <= 0:
            return self._reject("buy_signal_does_not_increase_position")
        if signal.action is TradeAction.SELL and desired_delta >= 0:
            return self._reject("sell_signal_does_not_reduce_position")

        order_notional = min(abs(desired_delta), self.limits.max_order_notional)
        if signal.action is TradeAction.BUY:
            order_notional = min(order_notional, portfolio.cash)
        if order_notional <= 0:
            return self._reject("insufficient_buying_power")

        quantity = order_notional / reference_price
        if signal.action is TradeAction.SELL and not self.limits.allow_short:
            quantity = min(quantity, max(current_quantity, Decimal("0")))
        if self.limits.allow_fractional_shares:
            quantity = quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        else:
            quantity = quantity.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if quantity <= 0:
            return self._reject("order_quantity_rounds_to_zero")

        signed_quantity = quantity if signal.action is TradeAction.BUY else -quantity
        projected_value = (current_quantity + signed_quantity) * reference_price
        if projected_value < 0 and not self.limits.allow_short:
            return self._reject("short_positions_are_disabled")
        projected_gross = portfolio.gross_exposure - abs(previous_value) + abs(projected_value)
        current_gross = portfolio.gross_exposure - abs(previous_value) + abs(current_value)
        if (
            projected_gross > marked_equity * self.limits.max_gross_exposure
            and projected_gross >= current_gross
        ):
            return self._reject("max_gross_exposure_exceeded")
        if (
            signal.action is TradeAction.BUY
            and abs(projected_value) > marked_equity * self.limits.max_position_weight
        ):
            return self._reject("max_position_weight_exceeded")

        order = OrderProposal(
            ticker=signal.ticker,
            action=signal.action,
            quantity=quantity,
            reference_price=reference_price,
            rationale=signal.thesis,
        )
        return RiskDecision(approved=True, order=order)

    def _pretrade_rejections(
        self,
        signal: AgentSignal,
        portfolio: PortfolioSnapshot,
        reference_price: Decimal,
        now: datetime,
    ) -> list[str]:
        reasons: list[str] = []
        if not reference_price.is_finite() or reference_price <= 0:
            return ["reference_price_must_be_finite_and_positive"]
        if now.tzinfo is None:
            raise ValueError("risk clock must be timezone-aware")
        age = (now - signal.generated_at).total_seconds()
        if age < 0 or age > self.limits.max_signal_age_seconds:
            reasons.append("signal_timestamp_out_of_bounds")
        if signal.action is TradeAction.HOLD:
            reasons.append("agent_requested_hold")
        if signal.confidence < self.limits.min_confidence:
            reasons.append("confidence_below_threshold")
        if (
            signal.action is TradeAction.BUY
            and signal.target_weight > self.limits.max_position_weight
        ):
            reasons.append("max_position_weight_exceeded")
        if (
            signal.action is TradeAction.BUY
            and portfolio.daily_pnl_fraction <= -self.limits.max_daily_loss_fraction
        ):
            reasons.append("daily_loss_limit_reached")
        if (
            signal.action is TradeAction.SELL
            and not self.limits.allow_short
            and portfolio.position_for(signal.ticker) is None
        ):
            reasons.append("cannot_sell_without_a_long_position")
        return reasons

    @staticmethod
    def _reject(reason: str) -> RiskDecision:
        return RiskDecision(approved=False, reasons=(reason,))
