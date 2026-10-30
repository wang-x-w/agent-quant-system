#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077

MODE=setup
case "${1:-}" in
    "") ;;
    --check) MODE=check ;;
    --help|-h)
        printf '%s\n' \
            'Usage: bash setup_research.sh [--check]' \
            'Prepare Python, dependencies and .env for TradingAgents research.' \
            '--check: inspect the existing environment without installing or editing files.' \
            'No model requests or broker connections are made.' \
            'Exit codes: 0 = local checks passed, 1 = setup failed, 2 = configuration needed.'
        exit 0
        ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
esac
if (( $# > 1 )); then
    printf 'Only one option is accepted. Use --help.\n' >&2
    exit 1
fi

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_ROOT"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
RESEARCH_PYTHON="$PROJECT_ROOT/.venv-research/bin/python"

if [[ "$MODE" == setup ]]; then
    trap 'printf "Setup failed at line %s. Resolve the error and rerun; no model was called.\n" "$LINENO" >&2; exit 1' ERR
    if ! command -v git >/dev/null 2>&1; then
        printf 'Git is required to install the pinned TradingAgents dependency. Install Git first.\n' >&2
        exit 1
    fi
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
    elif [[ -x "$HOME/.local/bin/uv" ]]; then
        UV="$HOME/.local/bin/uv"
    else
        printf 'Installing uv for the current user (requires Python 3 and pip)...\n'
        if ! command -v python3 >/dev/null 2>&1; then
            printf 'Install Python 3 and pip, or uv, then rerun this script. No sudo is used.\n' >&2
            exit 1
        fi
        python3 -m pip install --user uv
        UV="$(python3 -m site --user-base)/bin/uv"
        if [[ ! -x "$UV" ]]; then
            printf 'uv was not found after installation. Add its bin directory to PATH and rerun.\n' >&2
            exit 1
        fi
    fi
    if [[ -e .venv || -L .venv ]]; then
        if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; sys.exit(not ((3, 12) <= sys.version_info[:2] < (3, 15)))'; then
            printf 'Existing .venv is not a working Python 3.12-3.14 environment; it was not replaced.\n' >&2
            exit 1
        fi
    else
        printf 'Preparing Python 3.12 and the project virtual environment...\n'
        "$UV" python install 3.12
        "$UV" venv --python 3.12 "$PROJECT_ROOT/.venv"
    fi
    printf 'Installing locked project dependencies, including development, backtest and monitor extras...\n'
    UV_PROJECT_ENVIRONMENT="$PROJECT_ROOT/.venv" "$UV" sync --locked --inexact --all-extras --no-progress
    if [[ -e .venv-research || -L .venv-research ]]; then
        if [[ ! -x "$RESEARCH_PYTHON" ]] || ! "$RESEARCH_PYTHON" -c 'import sys; sys.exit(not ((3, 12) <= sys.version_info[:2] < (3, 15)))'; then
            printf 'Existing .venv-research is not a working Python 3.12-3.14 environment; it was not replaced.\n' >&2
            exit 1
        fi
    else
        "$UV" venv --python "$PYTHON" "$PROJECT_ROOT/.venv-research"
    fi
    printf 'Installing research dependencies separately to preserve the backtest pandas version...\n'
    "$UV" pip install --python "$RESEARCH_PYTHON" --editable '.[monitor]' -r requirements-agent.txt
    "$UV" pip check --python "$PYTHON"
    "$UV" pip check --python "$RESEARCH_PYTHON"
    trap - ERR
fi

if [[ ! -x "$RESEARCH_PYTHON" ]]; then
    printf 'Research Python is missing. Run bash setup_research.sh first.\n' >&2
    exit 1
fi

"$RESEARCH_PYTHON" - "$MODE" <<'PY'
import importlib.util
import os
import sys
from pathlib import Path

try:
    from dotenv import dotenv_values, load_dotenv, set_key
    from agent_quant_system.cli import doctor
    from agent_quant_system.config import Settings
except ImportError:
    print("Project dependencies are missing. Run bash setup_research.sh.")
    raise SystemExit(1) from None

env_path = Path(".env")
if env_path.is_symlink():
    print("Refusing a symlinked .env; use a regular project-local configuration file.")
    raise SystemExit(1)
if sys.argv[1] == "setup":
    if not env_path.exists():
        with env_path.open("x", encoding="utf-8") as target:
            target.write(Path(".env.example").read_text(encoding="utf-8"))
        print("Created .env from .env.example.")
    env_path.chmod(0o600)
    values = dotenv_values(env_path, interpolate=False)
    if values.get("AQS_RESEARCH_PROVIDER") in (None, "", "static"):
        set_key(env_path, "AQS_RESEARCH_PROVIDER", "tradingagents", quote_mode="never")
    print("Research configuration prepared; existing credentials and trading settings preserved.")
elif not env_path.is_file():
    print("Missing .env. Run bash setup_research.sh to initialize it.")
    raise SystemExit(2)

load_dotenv(env_path, override=False)
try:
    settings = Settings()
except Exception as error:
    print(f"Configuration validation failed ({type(error).__name__}); review .env locally.")
    raise SystemExit(2) from None

doctor()
problems = []
if settings.research_provider != "tradingagents":
    problems.append("Set AQS_RESEARCH_PROVIDER=tradingagents; exported shell variables override .env.")
if (settings.environment != "paper" or settings.execution_provider != "paper"
        or settings.live_trading_enabled or settings.paper_trading_enabled):
    problems.append("For research setup, use paper environment/execution and keep both trading flags false.")
if importlib.util.find_spec("tradingagents") is None:
    problems.append("TradingAgents is missing; rerun bash setup_research.sh.")
else:
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.portfolio import PortfolioContext
    except Exception as error:
        problems.append(f"TradingAgents import failed ({type(error).__name__}); check its dependencies.")

credential_names = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}
names = credential_names.get(settings.llm_provider.lower())
if names and not any(os.environ.get(name, "").strip() for name in names):
    problems.append(f"Set {' or '.join(names)} in .env locally. Never paste credentials into chat.")
elif names is None:
    print("Credential checks are not implemented for this provider; verify its authentication locally.")
if not settings.ib_account_id:
    print("IBKR is not configured: analyze works without it; research requires a paper account and Gateway/TWS.")
if problems:
    for problem in problems:
        print(f"NEEDS ACTION: {problem}")
    raise SystemExit(2)
print("Local preparation checks passed. API access, model availability and market data are not verified.")
print("No model requests, broker connections or orders were made.")
PY

printf '\nNext: run from %s (replace 200 with a verified reference price):\n' "$PROJECT_ROOT"
printf '%q run --no-project --env-file .env .venv-research/bin/agent-quant analyze --ticker AAPL --price 200 --cash 10000\n' "${UV:-uv}"