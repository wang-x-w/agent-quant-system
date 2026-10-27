from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from agent_quant_system.config import Settings
from agent_quant_system.models import AgentSignal, PortfolioSnapshot, Position, TradeAction
from agent_quant_system.research.execution import PaperExecutionGateway
from agent_quant_system.research.providers import (
    StaticResearchProvider,
    TradingAgentsResearchProvider,
)
from agent_quant_system.research.service import TradingSystem
from agent_quant_system.risk import RiskEngine


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _portfolio(ticker: str, cash: float, price: float, quantity: float) -> PortfolioSnapshot:
    reference_price = _decimal(price)
    position_quantity = _decimal(quantity)
    positions = {}
    if position_quantity:
        position = Position(
            ticker=ticker,
            quantity=position_quantity,
            market_price=reference_price,
        )
        positions[position.ticker] = position
    equity = _decimal(cash) + position_quantity * reference_price
    return PortfolioSnapshot(cash=_decimal(cash), equity=equity, positions=positions)


def _ensure_paper(settings: Settings) -> None:
    if settings.execution_provider != "paper" or settings.environment == "live":
        raise typer.BadParameter(
            "this MVP intentionally supports paper execution only; "
            "Nautilus/IBKR live wiring must be enabled in a reviewed deployment"
        )


def demo(
    ticker: str = typer.Option("AAPL", help="US equity ticker"),
    action: Annotated[TradeAction, typer.Option(case_sensitive=False)] = TradeAction.BUY,
    confidence: float = typer.Option(0.80, min=0, max=1),
    target_weight: float = typer.Option(0.05, min=0, max=1),
    price: float = typer.Option(200.0, min=0.01),
    cash: float = typer.Option(10_000.0, min=0),
    quantity: float = typer.Option(0.0, help="Existing position quantity"),
) -> None:
    """Run a deterministic end-to-end paper-trading example."""
    settings = Settings()
    _ensure_paper(settings)
    signal = AgentSignal(
        ticker=ticker,
        action=action,
        confidence=_decimal(confidence),
        target_weight=_decimal(target_weight),
        thesis="CLI deterministic demonstration signal",
        sources=("local-demo",),
    )
    portfolio = _portfolio(ticker, cash, price, quantity)
    system = TradingSystem(
        research=StaticResearchProvider(signal),
        risk=RiskEngine(settings.risk_limits()),
        execution=PaperExecutionGateway(),
    )
    result = system.run(ticker, portfolio, _decimal(price))
    typer.echo(result.model_dump_json(indent=2))


def analyze(
    ticker: str = typer.Option(..., help="US equity ticker"),
    price: float = typer.Option(..., min=0.01, help="Verified reference price"),
    cash: float = typer.Option(10_000.0, min=0),
    quantity: float = typer.Option(0.0, help="Existing position quantity"),
) -> None:
    """Ask TradingAgents for a proposal, then apply local risk and paper execution."""
    settings = Settings()
    _ensure_paper(settings)
    if settings.research_provider != "tradingagents":
        raise typer.BadParameter("set AQS_RESEARCH_PROVIDER=tradingagents to use this command")

    portfolio = _portfolio(ticker, cash, price, quantity)
    research = TradingAgentsResearchProvider(
        llm_provider=settings.llm_provider,
        quick_model=settings.quick_think_llm,
        deep_model=settings.deep_think_llm,
        target_weight=settings.agent_target_weight,
    )
    system = TradingSystem(
        research=research,
        risk=RiskEngine(settings.risk_limits()),
        execution=PaperExecutionGateway(),
    )
    result = system.run(ticker, portfolio, _decimal(price))
    typer.echo(result.model_dump_json(indent=2))


def research(
    ticker: str = typer.Option(..., help="Stock to research using TradingAgents"),
    output: Annotated[Path, typer.Option(help="Advisory report JSON path")] = Path(
        "reports/research.json"
    ),
) -> None:
    """Generate an advisory report from a read-only broker snapshot; never submit orders."""
    from agent_quant_system.trading.ibkr import IBKRPaperBroker

    settings = Settings()
    settings.guard_monitor(execute=False)
    if settings.research_provider != "tradingagents":
        raise typer.BadParameter("set AQS_RESEARCH_PROVIDER=tradingagents for research")
    if output.exists():
        raise typer.BadParameter("report already exists; choose a new output path")
    ticker = AgentSignal.normalize_ticker(ticker)
    research_settings = settings.model_copy(update={"ib_client_id": settings.ib_client_id + 100})
    broker = IBKRPaperBroker(research_settings, execute=False)
    try:
        broker.connect()
        snapshot = broker.snapshot((ticker,))
    finally:
        broker.disconnect()
    provider = TradingAgentsResearchProvider(
        llm_provider=settings.llm_provider,
        quick_model=settings.quick_think_llm,
        deep_model=settings.deep_think_llm,
        target_weight=settings.agent_target_weight,
    )
    signal = provider.propose(ticker, snapshot.portfolio)
    if signal.ticker != ticker:
        raise typer.BadParameter("research ticker does not match the request")
    payload = {
        "advisory_only": True,
        "automatic_execution": False,
        "snapshot_at": snapshot.observed_at.isoformat(),
        "signal": signal.model_dump(mode="json"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as report:
        json.dump(payload, report, ensure_ascii=False, indent=2)
    typer.echo(f"Advisory report written: {output}; no orders submitted.")
