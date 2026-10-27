from __future__ import annotations

import importlib.util
import json

import typer

from agent_quant_system.config import Settings
from agent_quant_system.research import cli as research_cli
from agent_quant_system.trading import cli as trading_cli

app = typer.Typer(no_args_is_help=True, help="Agent-assisted US equities paper trading")

app.command()(research_cli.demo)
app.command()(research_cli.analyze)


@app.command()
def doctor() -> None:
    """Report configuration and optional integration availability without exposing secrets."""
    settings = Settings()
    payload = {
        "environment": settings.environment,
        "research_provider": settings.research_provider,
        "execution_provider": settings.execution_provider,
        "live_trading_enabled": settings.live_trading_enabled,
        "tradingagents_installed": importlib.util.find_spec("tradingagents") is not None,
        "nautilus_trader_installed": importlib.util.find_spec("nautilus_trader") is not None,
        "ib_account_configured": bool(settings.ib_account_id),
        "ib_async_installed": importlib.util.find_spec("ib_async") is not None,
        "paper_trading_enabled": settings.paper_trading_enabled,
        "broker_connection_verified": False,
        "strategy_mode": settings.strategy_mode,
    }
    typer.echo(json.dumps(payload, indent=2))


app.command()(trading_cli.monitor)
app.command("monitor-status")(trading_cli.monitor_status)
app.command("monitor-stop")(trading_cli.monitor_stop)
app.command("monitor-flatten")(trading_cli.monitor_flatten)
app.command("monitor-resume")(trading_cli.monitor_resume)
app.command()(research_cli.research)


if __name__ == "__main__":
    app()
