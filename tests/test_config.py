import pytest
from pydantic import ValidationError

from agent_quant_system.config import Settings


def test_defaults_to_paper_execution() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "paper"
    assert settings.execution_provider == "paper"
    assert settings.live_trading_enabled is False


def test_live_mode_requires_explicit_confirmation() -> None:
    with pytest.raises(ValidationError, match="live trading requires"):
        Settings(environment="live", _env_file=None)


def test_nautilus_provider_requires_ib_account() -> None:
    with pytest.raises(ValidationError, match="AQS_IB_ACCOUNT_ID"):
        Settings(execution_provider="nautilus", _env_file=None)


def test_cli_commands_are_registered_from_their_domain() -> None:
    from agent_quant_system.cli import app

    commands = {
        command.name or command.callback.__name__: command.callback
        for command in app.registered_commands
    }
    research_commands = {"demo", "analyze", "research"}
    trading_commands = {
        "monitor",
        "monitor-status",
        "monitor-stop",
        "monitor-flatten",
        "monitor-resume",
    }
    assert set(commands) == research_commands | trading_commands | {"doctor"}
    for name in research_commands:
        assert commands[name].__module__ == "agent_quant_system.research.cli"
    for name in trading_commands:
        assert commands[name].__module__ == "agent_quant_system.trading.cli"
