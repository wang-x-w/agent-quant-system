import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


@pytest.fixture
def research_setup_project(tmp_path):
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "research project"
    project.mkdir()
    for name in ("setup_research.sh", ".env.example"):
        shutil.copyfile(source / name, project / name)
    (project / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (project / ".venv-research").symlink_to(sys.prefix, target_is_directory=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$*" >> "$SETUP_UV_LOG"\n'
        'if [[ "${SETUP_FAIL_INSTALL:-0}" == 1 ]]; then exit 9; fi\n'
        'if [[ "$1" == venv ]]; then ln -s "$SETUP_PYTHON_PREFIX" "${@: -1}"; fi\n'
    )
    uv.chmod(0o755)
    stubs = tmp_path / "stubs"
    graph = stubs / "tradingagents" / "graph"
    graph.mkdir(parents=True)
    (graph.parent / "__init__.py").write_text("")
    (graph / "__init__.py").write_text("")
    (graph / "trading_graph.py").write_text("class TradingAgentsGraph: pass\n")
    (graph.parent / "portfolio.py").write_text("class PortfolioContext: pass\n")
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith("AQS_") and not name.endswith("_API_KEY")
    }
    environment.update(
        PATH=f"{binaries}{os.pathsep}{os.environ['PATH']}",
        PYTHONPATH=str(stubs),
        SETUP_UV_LOG=str(tmp_path / "uv.log"),
        SETUP_PYTHON_PREFIX=sys.prefix,
    )

    def run(*arguments, **overrides):
        return subprocess.run(
            ["bash", str(project / "setup_research.sh"), *arguments],
            cwd=tmp_path,
            env=environment | overrides,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return project, tmp_path / "uv.log", run


def test_research_setup_initializes_config_without_model_calls(research_setup_project):
    project, log, run = research_setup_project

    result = run()

    assert result.returncode == 2, result.stdout + result.stderr
    assert "AQS_RESEARCH_PROVIDER=tradingagents" in (project / ".env").read_text()
    assert (project / ".env").stat().st_mode & 0o777 == 0o600
    assert "Set OPENAI_API_KEY" in result.stdout
    commands = log.read_text()
    assert "sync --locked --inexact --all-extras" in commands
    assert "--editable .[monitor] -r requirements-agent.txt" in commands
    assert ".venv-research/bin/python" in commands
    assert "-r requirements-agent.txt" in commands
    assert "pip check" in commands
    assert "analyze" not in commands


def test_research_setup_preserves_credentials_and_is_repeatable(research_setup_project):
    project, log, run = research_setup_project
    env_path = project / ".env"
    env_path.write_text(
        "AQS_RESEARCH_PROVIDER=static\nOPENAI_API_KEY='test-secret-not-for-display'\n"
        "AQS_MAX_ORDER_NOTIONAL=1234\nAQS_PAPER_TRADING_ENABLED=false\n"
    )

    first = run()
    prepared = env_path.read_text()
    second = run()
    commands = log.read_text()
    checked = run("--check")

    for result in (first, second, checked):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "test-secret-not-for-display" not in result.stdout + result.stderr
    assert "OPENAI_API_KEY='test-secret-not-for-display'" in prepared
    assert "AQS_MAX_ORDER_NOTIONAL=1234" in prepared
    assert "AQS_PAPER_TRADING_ENABLED=false" in prepared
    assert env_path.read_text() == prepared
    assert log.read_text() == commands


def test_research_setup_check_does_not_initialize(research_setup_project):
    project, log, run = research_setup_project

    result = run("--check")

    assert result.returncode == 2
    assert "Missing .env" in result.stdout
    assert not (project / ".env").exists()
    assert not log.exists()


def test_research_setup_install_failure_does_not_claim_success(research_setup_project):
    project, log, run = research_setup_project

    result = run(SETUP_FAIL_INSTALL="1")

    assert result.returncode != 0
    assert "Setup failed" in result.stderr
    assert not (project / ".env").exists()
    assert "requirements-agent.txt" not in log.read_text()


def test_research_setup_reports_shell_override(research_setup_project):
    _, _, run = research_setup_project

    result = run(AQS_RESEARCH_PROVIDER="static", OPENAI_API_KEY="test-placeholder")

    assert result.returncode == 2
    assert "exported shell variables override .env" in result.stdout


def test_research_setup_does_not_reset_trading_flags(research_setup_project):
    project, _, run = research_setup_project
    env_path = project / ".env"
    env_path.write_text("AQS_PAPER_TRADING_ENABLED=true\nOPENAI_API_KEY=test-placeholder\n")

    result = run()

    assert result.returncode == 2
    assert "keep both trading flags false" in result.stdout
    assert "AQS_PAPER_TRADING_ENABLED=true" in env_path.read_text()


def test_research_setup_help_is_side_effect_free(research_setup_project):
    project, log, run = research_setup_project

    result = run("--help")

    assert result.returncode == 0
    assert "--check" in result.stdout
    assert not log.exists()
    assert not (project / ".env").exists()


def test_research_setup_bootstraps_missing_virtual_environment(research_setup_project):
    project, log, run = research_setup_project
    (project / ".venv").unlink()
    (project / ".venv-research").unlink()

    result = run()

    assert result.returncode == 2, result.stdout + result.stderr
    assert (project / ".venv" / "bin" / "python").exists()
    assert (project / ".venv-research" / "bin" / "python").exists()
    commands = log.read_text()
    assert commands.count("venv --python") == 2
    assert commands.index("python install 3.12") < commands.index("venv --python 3.12")
    assert commands.index("venv --python 3.12") < commands.index("sync --locked")


@pytest.mark.parametrize("directory", [".venv", ".venv-research"])
def test_research_setup_preserves_incompatible_virtual_environment(
    research_setup_project, directory
):
    project, log, run = research_setup_project
    (project / directory).unlink()
    python = project / directory / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nexit 1\n")
    python.chmod(0o755)

    result = run()

    assert result.returncode == 1
    assert "it was not replaced" in result.stderr
    assert python.read_text() == "#!/usr/bin/env bash\nexit 1\n"
    assert not log.exists() or "pip install" not in log.read_text()


def test_research_setup_refuses_symlinked_config(research_setup_project):
    project, _, run = research_setup_project
    external = project.parent / "external.env"
    external.write_text("AQS_RESEARCH_PROVIDER=static\n")
    (project / ".env").symlink_to(external)

    result = run()

    assert result.returncode == 1
    assert "Refusing a symlinked .env" in result.stdout
    assert external.read_text() == "AQS_RESEARCH_PROVIDER=static\n"


@pytest.fixture
def installed_research_runtime(tmp_path):
    source = Path(__file__).resolve().parents[1]
    environment_root = source / ".venv-research"
    python = environment_root / "bin" / "python"
    if not python.is_file():
        pytest.skip("Run setup_research.sh to install the real research environment")
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith("AQS_") and not name.endswith("_API_KEY")
        and name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    probe = subprocess.run(
        [
            str(python), "-I", "-c",
            "import importlib.util, sys; "
            "sys.exit(0 if importlib.util.find_spec('tradingagents') else 3)",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode == 3:
        pytest.skip("TradingAgents dependencies have not finished installing")
    assert probe.returncode == 0, probe.stderr
    project = tmp_path / "real research runtime"
    project.mkdir()
    shutil.copyfile(source / "setup_research.sh", project / "setup_research.sh")
    (project / ".venv-research").symlink_to(environment_root, target_is_directory=True)
    env_path = project / ".env"
    env_path.write_text(
        "AQS_RESEARCH_PROVIDER=tradingagents\nAQS_LLM_PROVIDER=openai\n"
        "AQS_ENVIRONMENT=paper\nAQS_EXECUTION_PROVIDER=paper\n"
        "AQS_LIVE_TRADING_ENABLED=false\nAQS_PAPER_TRADING_ENABLED=false\n"
        "OPENAI_API_KEY=\n"
    )

    def run(*arguments):
        return subprocess.run(
            arguments,
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )

    return project, environment_root / "bin" / "agent-quant", run


def test_installed_research_cli_doctor_and_local_demo(installed_research_runtime):
    _, cli, run = installed_research_runtime

    doctor = run(str(cli), "doctor")
    assert doctor.returncode == 0, doctor.stderr
    diagnostics = json.loads(doctor.stdout)
    assert diagnostics["research_provider"] == "tradingagents"
    assert diagnostics["tradingagents_installed"] is True
    assert diagnostics["ib_async_installed"] is True
    assert diagnostics["paper_trading_enabled"] is False
    assert diagnostics["broker_connection_verified"] is False

    demo = run(str(cli), "demo", "--ticker", "AAPL", "--price", "200", "--cash", "10000")
    assert demo.returncode == 0, demo.stderr
    result = json.loads(demo.stdout)
    assert result["signal"]["sources"] == ["local-demo"]
    assert result["risk"]["approved"] is True
    assert result["execution"]["status"] == "FILLED"
    assert result["execution"]["broker_order_id"].startswith("paper-")
    rejected = run(str(cli), "demo", "--ticker", "AAPL", "--confidence", "0.3")
    assert rejected.returncode == 0, rejected.stderr
    rejection = json.loads(rejected.stdout)
    assert rejection["risk"]["approved"] is False
    assert rejection["execution"] is None


def test_installed_research_check_is_read_only_and_credentials_are_redacted(
    installed_research_runtime,
):
    project, _, run = installed_research_runtime
    env_path = project / ".env"
    before = env_path.read_bytes()
    before_stat = env_path.stat()
    script = str(project / "setup_research.sh")

    missing_key = run("bash", script, "--check")
    assert missing_key.returncode == 2, missing_key.stdout + missing_key.stderr
    assert "Set OPENAI_API_KEY" in missing_key.stdout
    assert "TradingAgents import failed" not in missing_key.stdout
    assert env_path.read_bytes() == before
    assert env_path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert env_path.stat().st_mode == before_stat.st_mode

    env_path.write_text(before.decode().replace("OPENAI_API_KEY=", "OPENAI_API_KEY=test-only-key"))
    configured = env_path.read_bytes()
    configured_stat = env_path.stat()
    for _ in range(2):
        checked = run("bash", script, "--check")
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "test-only-key" not in checked.stdout + checked.stderr
        assert "API access, model availability and market data are not verified" in checked.stdout
        assert env_path.read_bytes() == configured
        assert env_path.stat().st_mtime_ns == configured_stat.st_mtime_ns


def test_installed_research_rejects_enabled_trading_and_static_provider(installed_research_runtime):
    project, cli, run = installed_research_runtime
    env_path = project / ".env"
    env_path.write_text(
        "AQS_RESEARCH_PROVIDER=static\nAQS_PAPER_TRADING_ENABLED=true\n"
        "OPENAI_API_KEY=test-only-key\n"
    )
    before = env_path.read_bytes()

    checked = run("bash", str(project / "setup_research.sh"), "--check")
    assert checked.returncode == 2, checked.stdout + checked.stderr
    assert "Set AQS_RESEARCH_PROVIDER=tradingagents" in checked.stdout
    assert "keep both trading flags false" in checked.stdout
    assert env_path.read_bytes() == before
    refused = run(str(cli), "analyze", "--ticker", "AAPL", "--price", "200")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "AQS_RESEARCH_PROVIDER=tradingagents" in refused.stdout + refused.stderr
    assert "test-only-key" not in refused.stdout + refused.stderr
