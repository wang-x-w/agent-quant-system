from __future__ import annotations

import json
from pathlib import Path

import typer

from agent_quant_system.config import Settings


def monitor(
    once: bool = typer.Option(False, help="Run one monitoring cycle and exit"),
    execute: bool = typer.Option(False, help="Allow explicitly enabled IBKR PAPER orders"),
    mode: str | None = typer.Option(None, help="Strategy: daily or intraday; defaults to settings"),
) -> None:
    """Run daily or intraday IBKR monitoring; read-only unless --execute is supplied."""
    from agent_quant_system.trading.ibkr import IBKRPaperBroker
    from agent_quant_system.trading.intraday import IntradayMonitor
    from agent_quant_system.trading.monitor import (
        Journal,
        Monitor,
        deliver_notifications,
        monitor_lock,
    )

    settings = Settings()
    if mode is not None:
        if mode not in {"daily", "intraday"}:
            raise typer.BadParameter("mode must be daily or intraday")
        settings.strategy_mode = mode
    intraday = settings.strategy_mode == "intraday"
    if intraday and once and execute:
        raise typer.BadParameter("intraday execution requires a continuous monitor, not --once")
    try:
        settings.guard_monitor(execute=execute)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    account_lock = Path.home() / ".cache/agent-quant-system" / f"{settings.ib_account_id}.db"
    with monitor_lock(account_lock), monitor_lock(settings.monitor_state_path):
        journal = Journal(settings.monitor_state_path)
        broker = IBKRPaperBroker(settings, execute=execute)
        try:
            runner_type = IntradayMonitor if intraday else Monitor
            runner = runner_type(settings, broker, journal, execute=execute)
            failures = 0
            while True:
                try:
                    broker.connect()
                    if settings.monitor_stop_path.exists() or journal.get("halted"):
                        result = runner.cycle()
                    else:
                        try:
                            deliver_notifications(journal, settings)
                            if intraday:
                                runner.entries_allowed = True
                        except Exception:
                            if not intraday:
                                raise
                            runner.entries_allowed = False
                            typer.echo("Notification unavailable; new entries blocked.", err=True)
                        result = runner.cycle()
                    typer.echo(json.dumps({"status": result, "execute": execute}))
                    if result == "HALTED":
                        try:
                            deliver_notifications(journal, settings)
                        except Exception as exc:
                            typer.echo(
                                f"Stopped; notification pending: {type(exc).__name__}", err=True
                            )
                        return
                    try:
                        deliver_notifications(journal, settings)
                    except Exception:
                        if not intraday:
                            raise
                        runner.entries_allowed = False
                        typer.echo("Notification pending; protection remains active.", err=True)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    journal.event("cycle_failed", {"error_type": type(exc).__name__})
                    typer.echo(f"Cycle blocked: {type(exc).__name__}", err=True)
                    if journal.active():
                        journal.set("halted", "unresolved_submission")
                    broker.disconnect()
                    if once or failures >= 3:
                        if failures >= 3:
                            journal.set("halted", "repeated_failure")
                        raise typer.Exit(1) from exc
                if once:
                    return
                interval = (
                    settings.intraday_interval_seconds
                    if intraday
                    else settings.monitor_interval_seconds
                )
                for _ in range(interval):
                    if settings.monitor_stop_path.exists():
                        break
                    broker.wait(1)
        except KeyboardInterrupt:
            journal.set("halted", "keyboard_interrupt")
            try:
                runner.stop()
            except Exception as exc:
                typer.echo(
                    f"Stop persisted; broker cancellation unconfirmed: {type(exc).__name__}",
                    err=True,
                )
            raise typer.Exit(130) from None
        finally:
            broker.disconnect()
            journal.close()


def monitor_status() -> None:
    """Read the local audit state; this is not a fresh broker reconciliation."""
    from agent_quant_system.trading.monitor import Journal

    settings = Settings()
    if not settings.monitor_state_path.exists():
        typer.echo(
            json.dumps({"status": "NOT_STARTED", "stop_file": settings.monitor_stop_path.exists()})
        )
        return
    journal = Journal(settings.monitor_state_path)
    try:
        typer.echo(
            json.dumps(
                {
                    "halted": journal.get("halted"),
                    "stop_file": settings.monitor_stop_path.exists(),
                    "orders": journal.orders(),
                    "strategy_mode": journal.get("strategy_mode"),
                    "flatten_requested": journal.get("flatten_requested"),
                    "intraday_state": json.loads(journal.get("intraday_state") or "null"),
                },
                indent=2,
            )
        )
    finally:
        journal.close()


def monitor_stop() -> None:
    """Persist a stop request. Cancellation needs a connected execution-enabled monitor."""
    from agent_quant_system.trading.monitor import Journal

    settings = Settings()
    settings.monitor_stop_path.parent.mkdir(parents=True, exist_ok=True)
    settings.monitor_stop_path.touch(mode=0o600)
    journal = Journal(settings.monitor_state_path)
    try:
        journal.set("halted", "operator_stop")
        journal.event("stop_requested", {"broker_cancellation_confirmed": False})
    finally:
        journal.close()
    typer.echo("Stop persisted. Broker cancellation is NOT confirmed; verify open orders in TWS.")


def monitor_flatten(
    confirm_paper_exit: bool = typer.Option(
        False, help="Confirm managed intraday PAPER positions should exit"
    ),
) -> None:
    """Request a managed intraday exit; requires a running execution-enabled monitor."""
    from agent_quant_system.trading.monitor import Journal

    if not confirm_paper_exit:
        raise typer.BadParameter("pass --confirm-paper-exit to request paper position liquidation")
    settings = Settings()
    settings.guard_monitor(execute=True)
    journal = Journal(settings.monitor_state_path)
    try:
        journal.bind_account(settings.ib_account_id)
        if journal.get("strategy_mode") != "intraday":
            raise typer.BadParameter("flatten is supported only for an existing intraday state")
        journal.set("halted", "operator_flatten")
        journal.set("flatten_requested", "true")
        journal.event("flatten_requested", {"broker_execution_confirmed": False})
    finally:
        journal.close()
    typer.echo(
        "Paper exit requested, NOT confirmed. "
        "Run the intraday monitor with --execute and inspect TWS."
    )


def monitor_resume(
    confirm_reviewed: bool = typer.Option(
        False, help="Confirm you reviewed broker orders and state"
    ),
) -> None:
    """Clear the stop latch only after review and with no unresolved local orders."""
    from agent_quant_system.trading.monitor import Journal, monitor_lock

    if not confirm_reviewed:
        raise typer.BadParameter("review TWS and local state, then pass --confirm-reviewed")
    settings = Settings()
    settings.guard_monitor(execute=False)
    with monitor_lock(settings.monitor_state_path):
        journal = Journal(settings.monitor_state_path)
        try:
            journal.bind_account(settings.ib_account_id)
            if journal.get("flatten_requested"):
                raise typer.BadParameter("flatten request has not completed")
            if journal.active():
                raise typer.BadParameter("unresolved orders remain; reconcile before resuming")
            if journal.get("strategy_mode") == "intraday":
                from agent_quant_system.trading.intraday import IntradayState

                state = IntradayState.load(journal)
                if any(state.position(symbol) for symbol in state.symbols()):
                    raise typer.BadParameter(
                        "intraday positions remain; review and flatten before resume"
                    )
            journal.db.execute("DELETE FROM meta WHERE key='halted'")
            settings.monitor_stop_path.unlink(missing_ok=True)
            journal.event("resume_requested", {})
        finally:
            journal.close()
    typer.echo("Stop cleared. Next monitor cycle must reconcile with the broker before submitting.")
