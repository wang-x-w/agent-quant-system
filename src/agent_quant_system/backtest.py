"""Compatibility entry point for the backtest implementation in research.backtest."""

from agent_quant_system.research.backtest import (
    SYMBOLS,
    completed_sessions,
    fetch_prices,
    main,
    save_report,
    simulate,
    validate_prices,
)

__all__ = [
    "SYMBOLS",
    "completed_sessions",
    "fetch_prices",
    "main",
    "save_report",
    "simulate",
    "validate_prices",
]

if __name__ == "__main__":
    raise SystemExit(main())
