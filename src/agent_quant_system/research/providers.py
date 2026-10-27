from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from agent_quant_system.models import AgentSignal, PortfolioSnapshot, TradeAction

_RATINGS = {"BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL", "REVIEW"}


class StaticResearchProvider:
    def __init__(self, signal: AgentSignal) -> None:
        self.signal = signal

    def propose(self, ticker: str, portfolio: PortfolioSnapshot) -> AgentSignal:
        del ticker, portfolio
        return self.signal


class TradingAgentsResearchProvider:
    """Map TradingAgents research output into the system's strict signal contract."""

    def __init__(
        self,
        llm_provider: str,
        quick_model: str,
        deep_model: str,
        target_weight: Decimal = Decimal("0.05"),
        default_confidence: Decimal = Decimal("0"),
    ) -> None:
        try:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph
        except ImportError as exc:
            raise RuntimeError(
                "TradingAgents is not installed; install the agent optional dependency"
            ) from exc

        config = DEFAULT_CONFIG.copy()
        config.update(
            {
                "llm_provider": llm_provider,
                "quick_think_llm": quick_model,
                "deep_think_llm": deep_model,
            }
        )
        self._graph = TradingAgentsGraph(debug=False, config=config)
        self.target_weight = target_weight
        self.default_confidence = default_confidence

    def propose(self, ticker: str, portfolio: PortfolioSnapshot) -> AgentSignal:
        portfolio_context = self._portfolio_context(portfolio)
        state, decision = self._graph.propagate(
            ticker,
            datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
            portfolio=portfolio_context,
        )
        payload = self._jsonable(decision)
        rating = self._extract_rating(payload)
        confidence = self._extract_confidence(payload)
        action, target_weight = self._map_rating(rating, ticker, portfolio)
        thesis = json.dumps(
            {"rating": rating, "report": state.get("final_trade_decision", payload)},
            ensure_ascii=False,
            default=str,
        )
        return AgentSignal(
            ticker=ticker,
            action=action,
            confidence=confidence,
            target_weight=target_weight,
            thesis=thesis[:4_000],
            sources=("TradingAgents multi-agent report",),
        )

    @staticmethod
    def _portfolio_context(portfolio: PortfolioSnapshot) -> Any:
        try:
            from tradingagents.portfolio import PortfolioContext
        except ImportError as exc:
            raise RuntimeError("installed TradingAgents lacks PortfolioContext support") from exc

        return PortfolioContext.model_validate(
            {
                "cash": float(portfolio.cash),
                "currency": "USD",
                "positions": [
                    {
                        "ticker": position.ticker,
                        "quantity": float(position.quantity),
                        "average_price": (
                            float(position.average_cost)
                            if position.average_cost is not None
                            else None
                        ),
                    }
                    for position in portfolio.positions.values()
                ],
            }
        )

    @staticmethod
    def _jsonable(decision: Any) -> Any:
        if hasattr(decision, "model_dump"):
            return decision.model_dump(mode="json")
        if isinstance(decision, (dict, list, str, int, float, bool)) or decision is None:
            return decision
        return str(decision)

    @classmethod
    def _extract_rating(cls, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("rating", "action", "recommendation", "decision"):
                if key in payload:
                    payload = payload[key]
                    break
        if isinstance(payload, str) and payload.strip().upper() in _RATINGS:
            return payload.strip().upper()
        raise ValueError("TradingAgents output requires an exact recognized rating")

    def _extract_confidence(self, payload: Any) -> Decimal:
        if isinstance(payload, dict):
            for key in ("confidence", "confidence_score", "probability"):
                if key not in payload:
                    continue
                try:
                    confidence = Decimal(str(payload[key]))
                except Exception:
                    continue
                if not confidence.is_finite() or not 0 <= confidence <= 1:
                    raise ValueError("confidence must be finite and between zero and one")
                return confidence
        return self.default_confidence

    def _map_rating(
        self,
        rating: str,
        ticker: str,
        portfolio: PortfolioSnapshot,
    ) -> tuple[TradeAction, Decimal]:
        position = portfolio.position_for(ticker)
        current_weight = abs(position.market_value) / portfolio.equity if position else Decimal("0")
        if rating in {"BUY", "OVERWEIGHT"}:
            return TradeAction.BUY, self.target_weight
        if rating == "SELL":
            return TradeAction.SELL, Decimal("0")
        if rating == "UNDERWEIGHT" and current_weight > 0:
            return TradeAction.SELL, current_weight / Decimal("2")
        return TradeAction.HOLD, current_weight
