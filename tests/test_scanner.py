from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from sta.scanner.momentum import MomentumScanner, ScannerParams
from sta.scanner.features import compute_features


def _history(symbol, exchange, days=300, drift=0.0, breakout=True,
             volume=100_000, start=date(2024, 1, 1)):
    """Uptrending stock with flat early phase; optional final-day breakout."""
    n = days
    px = 100.0 + drift * np.arange(n)
    if breakout:
        px[-1] = px[-1] * 1.10  # gap up on the last day
    vol = np.full(n, volume, dtype=np.int64)
    if breakout:
        vol[-1] = volume * 4
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(n)], name="date")
    return pd.DataFrame({
        "symbol": symbol, "exchange": exchange, "isin": "INE000000000",
        "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
        "prev_close": np.roll(px, 1), "volume": vol, "value": px * vol,
        "series": "EQ", "name": symbol,
    }, index=idx)


def _membership(sym, start=date(2024, 5, 1), end=date(2025, 12, 31), exchange="NSE"):
    return pd.DataFrame([{
        "symbol": sym, "exchange": exchange, "isin": "INE000000000",
        "start": pd.Timestamp(start), "end": pd.Timestamp(end),
    }])


def test_breakout_symbol_is_scanned():
    strong = _history("STRONG", "NSE", drift=0.15)
    flat = _history("FLAT", "NSE", drift=0.0, volume=50_000)
    scanner = MomentumScanner({"STRONG": strong, "FLAT": flat},
                              _membership("STRONG") if False else pd.concat([
                                  _membership("STRONG"), _membership("FLAT")]))
    day = strong.index[-1].date()
    cands = scanner.scan(day)
    symbols = {c.symbol for c in cands}
    assert "STRONG" in symbols
    assert "FLAT" not in symbols


def test_membership_gate_blocks_non_member():
    hist = _history("OUT", "NSE", drift=0.15)
    # membership covers an earlier period only
    scanner = MomentumScanner({"OUT": hist}, _membership("OUT",
                                                             start=date(2024, 5, 1),
                                                             end=date(2024, 8, 1)))
    day = hist.index[-1].date()
    assert scanner.scan(day) == []


def test_features_shape_and_direction():
    hist = _history("T", "NSE", drift=0.15)
    feats = compute_features(hist)
    # close sits within 2% of the 52w-high on the breakout day (day's own
    # high is 1% above close by construction)
    assert feats["pct_from_high"].iloc[-1] > -0.02
    assert feats["vol_ratio"].iloc[-1] > 3      # surge day
    assert bool(feats["trend_ok"].iloc[-1])
