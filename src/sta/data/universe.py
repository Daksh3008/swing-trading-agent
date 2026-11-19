"""Tradeable-universe selection by market cap.

``Equity.csv`` (the reference project's BSE ticker list) carries no market-cap
column, so caps are fetched per ticker from yfinance, cached, and the list is
filtered to micro / small / mid caps up to ``max_mcap_cr`` (₹ crore).

Cap bands (₹ crore):
    micro : <  micro_upper_cr      (default  5,000)
    small : <  small_upper_cr      (default 25,000)
    mid   : <= max_mcap_cr         (default 50,000)

Failure taxonomy:
    ok          — market cap fetched
    dead        — Yahoo 404 (quote not found): permanent, never retried
    transient   — 401/429/auth hiccups: retried on later passes/resumes
    unavailable — still transient-failing after all retry passes
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from ..core.config import UniverseConfig

log = logging.getLogger(__name__)

CR = 10_000_000  # one crore in absolute rupees


def load_tickers(path: Path) -> list[str]:
    """Ticker list, one per line (reference Equity.csv format)."""
    tickers = [ln.strip() for ln in path.read_text().splitlines()
               if ln.strip() and not ln.startswith(",")]
    log.info("loaded %d tickers from %s", len(tickers), path)
    return tickers


def _failure_status(exc: Exception) -> str:
    """Classify a failure: 'dead' (permanent 404) vs 'transient' (401/429)."""
    text = str(exc)
    if ("404" in text or "Not Found" in text
            or "currentTradingPeriod" in text or "delisted" in text):
        return "dead"
    return "transient"


def _market_cap(ticker: str) -> tuple[float | None, str]:
    """Best-effort market cap. Returns ``(cap_rupees, status)``.

    ``t.info`` is tried first (one request); ``fast_info`` is the fallback
    (several requests). Status: ``ok`` / ``dead`` / ``transient``.
    """
    t = yf.Ticker(ticker)
    status = "transient"
    # fast_info first: derives cap = shares x last price from the chart +
    # fundamentals endpoints, which still serve the ~2,400 tickers purged
    # from quoteSummary.
    try:
        fi = t.fast_info
        cap = getattr(fi, "market_cap", None) or fi.get("market_cap")
        if cap:
            return float(cap), "ok"
    except Exception as exc:  # noqa: BLE001 — classify, then fall through
        status = _failure_status(exc)
    try:
        cap = t.info.get("marketCap")
        if cap:
            return float(cap), "ok"
    except Exception as exc:  # noqa: BLE001 — a 'dead' verdict is not downgraded
        if status != "dead":
            status = _failure_status(exc)
    return None, status


def fetch_market_caps(tickers: list[str], cache_path: Path,
                      workers: int = 6, sleep: float = 0.0,
                      retry_passes: int = 2) -> pd.DataFrame:
    """Fetch market caps for all tickers, resumable via ``cache_path``.

    Transient failures (401/429) are retried for ``retry_passes`` passes
    with a cool-down between passes; permanent 404s are marked ``dead``
    and never retried again. Returns columns: symbol, market_cap (₹),
    status (``ok``/``dead``/``unavailable``), fetched_at.
    """
    if cache_path.exists():
        cached = pd.read_parquet(cache_path).set_index("symbol")
        results: dict[str, dict] = cached.to_dict("index")
    else:
        results = {}

    def _todo() -> list[str]:
        return [t for t in tickers
                if t not in results
                or results[t]["status"] in ("unavailable", "transient")]

    for pass_no in range(retry_passes + 1):
        todo = _todo()
        if not todo:
            break
        log.info("pass %d: %d to (re)fetch", pass_no + 1, len(todo))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_market_cap, tk): tk for tk in todo}
            for fut in as_completed(futures):
                tk = futures[fut]
                cap, status = fut.result()
                results[tk] = {
                    "market_cap": cap,
                    "status": "ok" if cap is not None else status,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                }
                done += 1
                if done % 250 == 0:
                    log.info("pass %d: %d / %d fetched", pass_no + 1, done, len(todo))
                if sleep:
                    time.sleep(sleep)
        if pass_no < retry_passes:
            time.sleep(15)  # cool-down before hammering Yahoo again

    combined = (pd.DataFrame.from_dict(results, orient="index")
                .rename_axis("symbol"))
    combined = combined.loc[[t for t in tickers if t in combined.index]]
    combined.reset_index().to_parquet(cache_path, index=False)
    log.info("market caps cached: %d -> %s", len(combined), cache_path)
    return combined.reset_index()


def classify(cap_cr: float, cfg: UniverseConfig) -> str:
    if cap_cr < cfg.micro_upper_cr:
        return "micro"
    if cap_cr < cfg.small_upper_cr:
        return "small"
    return "mid"


def build_universe(tickers: list[str], caps: pd.DataFrame,
                   cfg: UniverseConfig) -> pd.DataFrame:
    """Filter to micro/small/mid caps <= ``max_mcap_cr`` and tag the band."""
    caps = caps.set_index("symbol").reindex(tickers)
    keep = caps[(caps["status"] == "ok")
                & (caps["market_cap"] <= cfg.max_mcap_cr * CR)]
    membership = keep.reset_index()[["symbol", "market_cap"]].copy()
    membership["exchange"] = "BSE"
    membership["mcap_cr"] = (membership["market_cap"] / CR).round(1)
    membership["cap_class"] = membership["mcap_cr"].map(
        lambda c: classify(c, cfg))
    membership = (membership
                  .drop(columns="market_cap")
                  .sort_values("mcap_cr", ascending=False)
                  .reset_index(drop=True))
    counts = membership["cap_class"].value_counts().to_dict()
    log.info("universe: %d symbols %s (mcap <= %s cr)",
             len(membership), counts, cfg.max_mcap_cr)
    return membership[["symbol", "exchange", "mcap_cr", "cap_class"]]


def membership_spans(membership: pd.DataFrame,
                     histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Scanner membership frame: ``symbol, exchange, isin, start, end``.

    The yfinance pipeline has one row per symbol (no start/end spans), so each
    symbol's span is synthesized from its own history coverage.
    """
    if membership is None or membership.empty:
        return pd.DataFrame(columns=["symbol", "exchange", "isin", "start", "end"])
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
