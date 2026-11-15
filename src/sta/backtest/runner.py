"""Backtest orchestration: store + membership + scanner -> engine -> report.

Membership from the yfinance pipeline has one row per symbol (no start/end
spans); spans are synthesized from each symbol's own history coverage.
"""

from __future__ import annotations

import json
import logging
from datetime import date as Date, datetime

import pandas as pd

from ..backtest.costs import CostModel
from ..backtest.engine import BacktestEngine, StrategyParams
from ..core.config import Settings
from ..data.store import ParquetBarStore
from ..scanner.momentum import MomentumScanner, ScannerParams

log = logging.getLogger(__name__)


def _membership_spans(store: ParquetBarStore, membership: pd.DataFrame,
                      histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Synthesize scanner spans (symbol, exchange, isin, start, end)."""
    if {"start", "end"}.issubset(membership.columns):
        return membership
    rows = []
    for sym in membership["symbol"].astype(str):
        hist = histories.get(sym)
        if hist is None or hist.empty:
            continue
        rows.append({
            "symbol": sym,
            "exchange": str(hist["exchange"].iloc[0]) if "exchange" in hist else "BSE",
            "isin": "",
            "start": hist.index[0].date(),
            "end": hist.index[-1].date(),
        })
    return pd.DataFrame(rows)


def run_backtest(settings: Settings, start: str, end: str,
                 strategy: StrategyParams | None = None,
                 scanner_params: ScannerParams | None = None,
                 costs: CostModel | None = None,
                 out_dir: str | None = None,
                 symbols_limit: int | None = None) -> dict:
    store = ParquetBarStore(settings.data_dir)
    membership_path = settings.universe_dir / "membership.parquet"
    if not membership_path.exists():
        raise RuntimeError("run `python -m sta universe` first — membership missing")
    membership = pd.read_parquet(membership_path)

    member_symbols = sorted(set(membership["symbol"].astype(str)))
    if symbols_limit:
        member_symbols = member_symbols[:symbols_limit]
    log.info("loading histories for %d member symbols", len(member_symbols))

    histories: dict[str, pd.DataFrame] = {}
    for symbol in member_symbols:
        hist = store.load_symbol(symbol)
        if hist is not None and not hist.empty:
            histories[symbol] = hist
    log.info("loaded %d histories", len(histories))

    spans = _membership_spans(store, membership, histories)

    days = [d for d in store.available_days() if _parse(start) <= d <= _parse(end)]
    if len(days) < 200:
        raise RuntimeError(f"only {len(days)} trading days in range — download more data")

    scanner = MomentumScanner(histories, spans, scanner_params)
    engine = BacktestEngine(scanner, bars=histories, trading_days=days,
                            params=strategy, costs=costs)
    log.info("running backtest over %d days", len(days))
    result = engine.run()

    summary = result.summary()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = settings.data_dir / (out_dir or f"backtest_{stamp}")
    target.mkdir(parents=True, exist_ok=True)
    result.nav.to_frame("nav").to_parquet(target / "nav.parquet")
    result.daily_exposure.to_frame("exposure").to_parquet(target / "exposure.parquet")
    trades = pd.DataFrame([vars(t) | {"pnl": t.pnl, "return_pct": t.return_pct,
                                       "exit_reason": str(t.exit_reason)}
                           for t in result.trades])
    trades.to_parquet(target / "trades.parquet")
    with open(target / "summary.json", "w") as fh:
        json.dump({k: (v if not isinstance(v, float) or abs(v) < 1e15 else str(v))
                   for k, v in summary.items()}, fh, indent=2)
    log.info("results written to %s", target)
    return summary


def _parse(text: str) -> Date:
    return datetime.strptime(text, "%Y-%m-%d").date()
