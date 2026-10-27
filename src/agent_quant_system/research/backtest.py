from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

SYMBOLS = {"AAPL": "105.AAPL", "MSFT": "105.MSFT", "NVDA": "105.NVDA",
           "AMZN": "105.AMZN", "GOOGL": "105.GOOGL", "SPY": "107.SPY"}


def fetch_prices(ticker: str, start: str, end: str) -> tuple[list[dict], dict]:
    params = {"secid": SYMBOLS[ticker], "klt": "101", "fqt": "1",
              "beg": start.replace("-", ""), "end": end.replace("-", ""),
              "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56"}
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode(params, safe=",")
    response = subprocess.run(
        ["curl", "--http1.1", "--fail", "--silent", "--show-error", "--max-time", "25",
         "--retry", "2", "-A", "Mozilla/5.0", url],
        capture_output=True, text=True, check=True, timeout=90,
    )
    payload = json.loads(response.stdout)
    data = payload.get("data")
    if not data or data.get("code") != ticker or not data.get("klines"):
        raise ValueError(f"Missing or mismatched market data for {ticker}")
    rows = []
    for fields in csv.reader(io.StringIO("\n".join(data["klines"]))):
        date, opening, close, high, low, volume = fields
        datetime.strptime(date, "%Y-%m-%d")
        rows.append({"Date": date, "Open": float(opening), "High": float(high),
                     "Low": float(low), "Close": float(close), "Volume": float(volume)})
    return rows, {"url": url, "response": payload}


def completed_sessions(as_of: datetime):
    import exchange_calendars as xcals
    import pandas as pd

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    calendar = xcals.get_calendar("XNYS")
    now = pd.Timestamp(as_of)
    candidates = calendar.sessions_in_range(
        (as_of - timedelta(days=90)).date(), now.tz_convert("America/New_York").date()
    )
    completed = [session for session in candidates if calendar.session_close(session) < now]
    if len(completed) < 25:
        raise ValueError("Need 20 warm-up sessions and five completed sessions")
    return pd.DatetimeIndex(completed[-25:]).tz_localize(None)


def validate_prices(frame, sessions):
    import numpy as np
    import pandas as pd

    columns = ["Open", "High", "Low", "Close", "Volume"]
    frame = frame.copy()
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
    if frame.index.has_duplicates or len(sessions.difference(frame.index)):
        raise ValueError("Missing or duplicate required sessions")
    frame = frame.loc[sessions, columns].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(frame.to_numpy()).all() or (frame <= 0).any().any():
        raise ValueError("OHLCV must be finite and positive")
    if (frame["High"] < frame[["Open", "Close", "Low"]].max(axis=1)).any():
        raise ValueError("Inconsistent high price")
    if (frame["Low"] > frame[["Open", "Close", "High"]].min(axis=1)).any():
        raise ValueError("Inconsistent low price")
    return frame


def simulate(prices, sessions, initial_cash=10000.0):
    import backtrader as bt
    import exchange_calendars as xcals
    import pandas as pd

    from agent_quant_system.models import (
        AgentSignal,
        PortfolioSnapshot,
        Position,
        RiskLimits,
        TradeAction,
    )
    from agent_quant_system.risk import RiskEngine

    if not Decimal(str(initial_cash)).is_finite() or initial_cash <= 0:
        raise ValueError("Initial cash must be finite and positive")
    if "SPY" not in prices or len(prices) < 2 or len(sessions) != 25:
        raise ValueError("Need SPY, at least one stock, and exactly 25 sessions")
    prices = {ticker: validate_prices(frame, sessions) for ticker, frame in prices.items()}
    trade_dates = [session.date() for session in sessions[-5:]]
    calendar = xcals.get_calendar("XNYS")

    class WeeklyStrategy(bt.Strategy):
        def __init__(self):
            self.engine = RiskEngine(RiskLimits(allow_fractional_shares=False))
            self.averages = {feed._name: bt.ind.SMA(feed.close, period=20) for feed in self.datas}
            self.curve, self.decisions, self.fills, self.failures = [], [], [], []
            self.previous_equity = initial_cash

        def notify_order(self, order):
            if order.status in (order.Submitted, order.Accepted):
                return
            if order.status == order.Completed:
                self.fills.append({
                    "date": bt.num2date(order.executed.dt).date().isoformat(),
                    "signal_date": order.info.signal_date, "ticker": order.data._name,
                    "action": "BUY" if order.isbuy() else "SELL",
                    "quantity": abs(order.executed.size), "price": order.executed.price,
                    "notional": abs(order.executed.size) * order.executed.price,
                    "commission": order.executed.comm,
                })
            else:
                self.failures.append({"ticker": order.data._name, "status": order.getstatusname()})

        def next(self):
            current_date = self.datas[0].datetime.date(0)
            cash, equity = self.broker.getcash(), self.broker.getvalue()
            positions = {
                feed._name: Position(
                    ticker=feed._name, quantity=Decimal(str(self.getposition(feed).size)),
                    market_price=Decimal(str(feed.close[0])),
                ) for feed in self.datas if self.getposition(feed).size
            }
            if cash < -1e-8 or any(position.quantity < 0 for position in positions.values()):
                raise RuntimeError("Cash/long-only invariant violated")
            marked_value = sum(float(position.market_value) for position in positions.values())
            if abs(cash + marked_value - equity) > 1e-6:
                raise RuntimeError("Equity does not reconcile with cash and positions")
            portfolio = PortfolioSnapshot(
                cash=Decimal(str(max(cash, 0))), equity=Decimal(str(equity)), positions=positions,
                daily_pnl_fraction=Decimal(str(equity / self.previous_equity - 1)),
            )
            self.previous_equity = equity
            if current_date in trade_dates:
                self.curve.append({
                    "date": current_date.isoformat(), "equity": equity, "cash": cash,
                    **{f"shares_{feed._name}": self.getposition(feed).size for feed in self.datas},
                })
            if current_date >= trade_dates[-1]:
                return
            for feed in self.datas:
                ticker = feed._name
                if ticker == "SPY":
                    continue
                bullish = feed.close[0] > self.averages[ticker][0]
                action = TradeAction.BUY if bullish else TradeAction.SELL
                signal = AgentSignal(
                    ticker=ticker, action=action, confidence=Decimal("1"),
                    target_weight=Decimal("0.05") if bullish else Decimal("0"),
                    thesis="Deterministic SMA20 rule; confidence is NOT an AI probability",
                    generated_at=calendar.session_close(pd.Timestamp(current_date)).to_pydatetime(),
                )
                decision = self.engine.evaluate(
                    signal, portfolio, Decimal(str(feed.close[0])), now=signal.generated_at,
                )
                self.decisions.append({
                    "date": current_date.isoformat(), "ticker": ticker, "action": action.value,
                    "approved": decision.approved, "reasons": ";".join(decision.reasons),
                    "reference_close": feed.close[0], "sma20": self.averages[ticker][0],
                })
                if not decision.approved:
                    continue
                quantity = int(decision.order.quantity)
                if action is TradeAction.SELL:
                    quantity = min(quantity, self.getposition(feed).size)
                    order = self.sell(data=feed, size=quantity)
                else:
                    order = self.buy(data=feed, size=quantity)
                order.addinfo(signal_date=current_date.isoformat())

    cerebro = bt.Cerebro(stdstats=False)
    for ticker, frame in prices.items():
        cerebro.adddata(bt.feeds.PandasData(dataname=frame), name=ticker)
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=0.0005)
    cerebro.broker.set_slippage_perc(0.0005, slip_open=True, slip_match=True)
    cerebro.addstrategy(WeeklyStrategy)
    strategy = cerebro.run()[0]
    curve = pd.DataFrame(strategy.curve)
    if len(curve) != 5:
        raise RuntimeError("Expected five daily account snapshots")
    benchmark = prices["SPY"].loc[sessions[-5:]]
    curve["spy_equity"] = initial_cash * benchmark["Close"].to_numpy() / benchmark["Open"].iloc[0]
    baseline = {column: 0.0 for column in curve.columns if column != "date"}
    baseline.update(date="Initial", equity=initial_cash, cash=initial_cash, spy_equity=initial_cash)
    curve = pd.concat([pd.DataFrame([baseline]), curve], ignore_index=True)
    curve["drawdown_pct"] = (curve["equity"] / curve["equity"].cummax() - 1) * 100
    curve["spy_drawdown_pct"] = (curve["spy_equity"] / curve["spy_equity"].cummax() - 1) * 100
    return curve, strategy


def save_report(output, prices, sessions, curve, strategy, provenance):
    import matplotlib
    import pandas as pd

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    curve.to_csv(output / "equity.csv", index=False)
    pd.DataFrame(strategy.fills, columns=[
        "date", "signal_date", "ticker", "action", "quantity", "price", "notional", "commission",
    ]).to_csv(output / "trades.csv", index=False)
    pd.DataFrame(strategy.decisions).to_csv(output / "decisions.csv", index=False)
    initial, final = float(curve["equity"].iloc[0]), float(curve["equity"].iloc[-1])
    summary = {
        "status": "COMPLETED", "data": provenance,
        "start_date": sessions[-5].date().isoformat(),
        "end_date": sessions[-1].date().isoformat(),
        "initial_equity": initial, "final_equity": final, "pnl_usd": final - initial,
        "return_pct": (final / initial - 1) * 100,
        "spy_return_pct": (float(curve["spy_equity"].iloc[-1]) / initial - 1) * 100,
        "max_daily_drawdown_pct": -float(curve["drawdown_pct"].min()),
        "fills": len(strategy.fills),
        "commissions_usd": sum(fill["commission"] for fill in strategy.fills),
        "broker_failures": strategy.failures,
        "assumptions": [
            "SMA20 deterministic baseline, NOT TradingAgents or LLM performance",
            "Previous close signal and risk sizing; next open execution; integer shares",
            "5% target per stock, at most 25% target stock allocation for five stocks",
            "Default RiskLimits, fractional shares disabled; core risk defects are NOT repaired",
            "5bps commission per side; 5bps slippage capped by daily high/low",
            "Eastmoney forward-adjusted daily prices; no separate dividend cash flows",
            "SPY gross adjusted-price benchmark enters at first session open without costs",
            "Final positions marked to closing prices, not liquidated; cash earns zero interest",
            "Drawdown sampled at daily closes; five days cannot establish strategy profitability",
            "Fixed five-stock universe, not the entire US market; no annualized metrics",
            "Backtrader broker replaces the smoke-test PaperExecutionGateway for accounting",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with plt.rc_context({"font.family": "DejaVu Sans", "axes.spines.top": False,
                         "axes.spines.right": False, "figure.facecolor": "#f6f8fa"}):
        figure, axes = plt.subplots(3, 1, figsize=(12, 11), constrained_layout=True)
        figure.suptitle(
            f"US Equities | {summary['start_date']} to {summary['end_date']}\n"
            "SMA20 + project risk engine | NOT an AI strategy backtest", fontsize=16,
        )
        axes[0].plot(curve["date"], curve["equity"], "o-", color="#167d8d", label="Strategy (net)")
        axes[0].plot(curve["date"], curve["spy_equity"], "o-", color="#be6040", label="SPY (gross)")
        axes[0].plot(curve["date"], initial * 0.75 + curve["spy_equity"] * 0.25,
                     "--", color="#68766b", label="25% SPY + 75% cash (gross)")
        axes[0].set_ylabel("Account value (USD)")
        axes[1].plot(curve["date"], curve["drawdown_pct"], "o-", color="#167d8d", label="Strategy")
        axes[1].plot(curve["date"], curve["spy_drawdown_pct"], "o-", color="#be6040", label="SPY")
        axes[1].set_ylabel("Daily-close drawdown (%)")
        for ticker, frame in prices.items():
            week = frame.loc[sessions[-5:]]
            normalized = [100.0, *(week["Close"] / week["Open"].iloc[0] * 100).tolist()]
            axes[2].plot(curve["date"], normalized, marker=".", label=ticker)
        axes[2].set_ylabel("Adjusted price index (start = 100)")
        for axis in axes:
            axis.grid(alpha=0.2)
            axis.legend(loc="best", ncol=3)
            axis.tick_params(axis="x", labelsize=9)
        figure.savefig(output / "curves.png", dpi=160)
        plt.close(figure)
    rows = "\n".join(
        f"| {row.date} | {row.equity:.2f} | {row.cash:.2f} | {row.spy_equity:.2f} |"
        for row in curve.itertuples()
    )
    text = (
        "# Weekly US Equities Simulation\n\n"
        f"Period: {summary['start_date']} to {summary['end_date']}\n\n"
        f"Net P&L: USD {summary['pnl_usd']:.2f}; return: {summary['return_pct']:.4f}%; "
        f"SPY gross return: {summary['spy_return_pct']:.4f}%.\n\n"
        f"Fills: {summary['fills']}; commissions: USD {summary['commissions_usd']:.4f}; "
        f"max daily-close drawdown: {summary['max_daily_drawdown_pct']:.4f}%.\n\n"
        "![Equity, drawdown and prices](curves.png)\n\n"
        "| Date | Strategy equity | Cash | SPY gross equity |\n|---|---:|---:|---:|\n"
        + rows + "\n\n## Assumptions\n\n"
        + "\n".join(f"- {item}" for item in summary["assumptions"])
        + "\n\nSource URLs, retrieval time and CSV SHA256 hashes are in summary.json.\n"
        "Original provider responses, normalized prices, trades and decisions "
        "accompany this report.\n"
    )
    (output / "report.md").write_text(text, encoding="utf-8")
    return summary


def main():
    import pandas as pd

    parser = argparse.ArgumentParser(
        description="Last five completed US sessions; no fake fallback",
    )
    parser.add_argument("--as-of", default=datetime.now(UTC).isoformat())
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument(
        "--data-dir", type=Path, help="Replay saved Date,Open,High,Low,Close,Volume CSVs",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of)
    sessions = completed_sessions(as_of)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output / f"weekly-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    prices, errors = {}, {}
    provenance = {
        "as_of": as_of.isoformat(), "retrieved_at": datetime.now(UTC).isoformat(),
        "source": "Local adjusted-price CSVs" if args.data_dir else "Eastmoney daily API, fqt=1",
        "required_sessions": sessions.strftime("%Y-%m-%d").tolist(), "files": {},
    }
    print(f"Test window: {sessions[-5].date()} through {sessions[-1].date()}", flush=True)
    for ticker in SYMBOLS:
        try:
            if args.data_dir:
                frame = pd.read_csv(
                    args.data_dir / f"{ticker}.csv", index_col="Date", parse_dates=True,
                )
                source = {"url": str(args.data_dir / f"{ticker}.csv")}
            else:
                download_start = sessions[0].date().replace(day=1).isoformat()
                rows, source = fetch_prices(ticker, download_start, str(sessions[-1].date()))
                frame = pd.DataFrame(rows).set_index("Date")
                (output / f"{ticker}.source.json").write_text(
                    json.dumps(source, indent=2, ensure_ascii=False), encoding="utf-8",
                )
            prices[ticker] = validate_prices(frame, sessions)
            csv_text = prices[ticker].to_csv(index_label="Date")
            (output / f"{ticker}.csv").write_text(csv_text, encoding="utf-8")
            provenance["files"][ticker] = {
                "sha256": hashlib.sha256(csv_text.encode()).hexdigest(), "source": source["url"],
            }
            print(f"{ticker}: validated {len(prices[ticker])} sessions", flush=True)
        except Exception as error:
            errors[ticker] = f"{type(error).__name__}: {error}"
            print(f"{ticker}: {errors[ticker]}", flush=True)
    if errors:
        failure = {"status": "BLOCKED", "data": provenance, "errors": errors,
                   "reason": "Missing verified prices; no returns or curves fabricated"}
        (output / "summary.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
        print(f"Blocked. Evidence: {output / 'summary.json'}", flush=True)
        return 2
    curve, strategy = simulate(prices, sessions)
    summary = save_report(output, prices, sessions, curve, strategy, provenance)
    print(json.dumps({key: value for key, value in summary.items() if key != "data"}, indent=2))
    print(f"Report: {output / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())