from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from agent_quant_system.models import (
    AgentSignal,
    ExecutionReceipt,
    PortfolioSnapshot,
    RiskDecision,
)
from agent_quant_system.risk import RiskEngine


class ResearchProvider(Protocol):
    def propose(self, ticker: str, portfolio: PortfolioSnapshot) -> AgentSignal: ...


class ExecutionGateway(Protocol):
    def submit(self, decision: RiskDecision) -> ExecutionReceipt: ...


class TradingRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_ticker: str
    signal: AgentSignal
    risk: RiskDecision
    execution: ExecutionReceipt | None = None


class TradingSystem:
    def __init__(
        self,
        research: ResearchProvider,
        risk: RiskEngine,
        execution: ExecutionGateway,
    ) -> None:
        self.research = research
        self.risk = risk
        self.execution = execution

    def run(
        self,
        ticker: str,
        portfolio: PortfolioSnapshot,
        reference_price: Decimal,
    ) -> TradingRun:
        requested_ticker = ticker.strip().upper()
        signal = self.research.propose(requested_ticker, portfolio)
        if signal.ticker != requested_ticker:
            decision = RiskDecision(approved=False, reasons=("research_ticker_mismatch",))
            return TradingRun(
                requested_ticker=requested_ticker,
                signal=signal,
                risk=decision,
            )

        decision = self.risk.evaluate(signal, portfolio, reference_price)
        receipt = self.execution.submit(decision) if decision.approved else None
        return TradingRun(
            requested_ticker=requested_ticker,
            signal=signal,
            risk=decision,
            execution=receipt,
        )
