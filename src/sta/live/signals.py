"""Daily signal generation: scan the latest session and persist suggestions.

A suggestion is a *plan*, not a fill: it records the signal-day close, the
planned entry (next session's open), the initial stop, the share count for the
configured position size, the rupee risk, and the distance to the 52-week high.
The tracker is what turns a suggestion into a paper position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime

import pandas as pd

from ..core.config import Settings
from ..data.store import ParquetBarStore
from ..data.sync import last_stored_date
from ..data.universe import membership_spans
from ..scanner.momentum import MomentumScanner, ScannerParams
from .db import Database
from .rules import initial_stop, shares_for
from .settings import LiveSettings, load_live_settings

log = logging.getLogger(__name__)


@dataclass
class ScanOutcome:
    day: Date | None
    scanned: int = 0
    candidates: list[dict] = field(default_factory=list)
    stored: int = 0
    tracked: int = 0
    skipped: int = 0
    missing_data: bool = False

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat() if self.day else None,
            "scanned": self.scanned,
            "candidates": len(self.candidates),
            "stored": self.stored,
            "tracked": self.tracked,
            "skipped": self.skipped,
            "missing_data": self.missing_data,
        }


# ------------------------------------------------------------------- helpers


def latest_session(settings: Settings, db: Database | None = None) -> Date | None:
    """Newest trading day the cache knows about."""
    if db is not None:
        day = db.last_day()
        if day:
            return datetime.strptime(day, "%Y-%m-%d").date()
    files = sorted(settings.daily_dir.glob("*.parquet"))
    if not files:
        return None
    sample = files[:: max(1, len(files) // 20)][:20]
    dates = [d for d in (last_stored_date(p) for p in sample) if d]
    return max(dates) if dates else None


def load_histories(settings: Settings, symbols: list[str] | None = None,
                   tail: int | None = None) -> dict[str, pd.DataFrame]:
    store = ParquetBarStore(settings.data_dir)
    universe = symbols if symbols is not None else store.list_symbols()
    out: dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(universe, 1):
        hist = store.load_symbol(sym, tail=tail)
        if hist is not None and len(hist) >= 60:
            out[sym] = hist
        if i % 500 == 0:
            log.info("loaded %d/%d histories", i, len(universe))
    return out


def high_distance(hist: pd.DataFrame, day: Date) -> float | None:
    """Close vs the 52-week high on ``day`` (0 = at the high, -0.08 = 8% below)."""
    try:
        closes = hist["close"]
        window = closes.loc[: pd.Timestamp(day)].tail(252)
        highs = hist["high"].loc[: pd.Timestamp(day)].tail(252)
    except KeyError:
        return None
    if window.empty or highs.empty:
        return None
    peak = float(highs.max())
    if peak <= 0:
        return None
    return float(window.iloc[-1] / peak - 1.0)


def build_rows(candidates, histories: dict[str, pd.DataFrame],
               cap_class: dict[str, str], live: LiveSettings,
               day: Date, auto_track: bool) -> list[dict]:
    rows: list[dict] = []
    for cand in candidates:
        close = float(cand.close)
        qty = shares_for(live.position_size, close)
        if qty <= 0:
            continue
        stop = initial_stop(close, live.stop_pct)
        rows.append({
            "signal_date": day,
            "symbol": cand.symbol,
            "exchange": cand.exchange.value,
            "name": cand.name or cand.symbol,
            "score": float(cand.score),
            "close": close,
            "cap_class": cap_class.get(cand.symbol, ""),
            "entry_plan": close,
            "stop_initial": stop,
            "qty": qty,
            "risk": (close - stop) * qty,
            "dist_high": high_distance(histories[cand.symbol], day),
            "action": "track" if auto_track else "new",
        })
    return rows


# ---------------------------------------------------------------------- scan


def run_scan(settings: Settings, db: Database, live: LiveSettings | None = None,
             day: Date | str | None = None, top_n: int | None = None,
             commit: bool = True) -> ScanOutcome:
    """Scan the latest (or given) session. Idempotent per date."""
    live = live or load_live_settings(settings, db)
    day = _as_date(day) or latest_session(settings, db)
    if day is None:
        return ScanOutcome(day=None, missing_data=True)

    membership_path = settings.universe_dir / "membership.parquet"
    if not membership_path.exists():
        raise RuntimeError("membership.parquet missing — run `python -m sta universe`")
    membership = pd.read_parquet(membership_path)
    caps = dict(zip(membership["symbol"].astype(str),
                    membership["cap_class"], strict=False))

    flagged = db.flagged_symbols()
    symbols = [s for s in membership["symbol"].astype(str) if s not in flagged]
    tail = settings.live.scan_tail_bars
    histories = load_histories(settings, symbols, tail=tail)
    # keep only symbols that actually printed on the requested day
    histories = {s: h for s, h in histories.items() if pd.Timestamp(day) in h.index}
    if not histories:
        return ScanOutcome(day=day, missing_data=True)

    # A partial feed would rank a handful of symbols and invent a signal.
    if len(histories) < 0.5 * max(1, len(symbols)):
        raise ValueError(
            f"only {len(histories)} of {len(symbols)} symbols have a bar for "
            f"{day} — the feed looks incomplete, refusing to scan")

    spans = membership_spans(membership, histories)
    params = ScannerParams(max_candidates=top_n or live.scan_top_n)
    scanner = MomentumScanner(histories, spans, params)
    candidates = scanner.scan(day)

    rows = build_rows(candidates, histories, caps, live, day,
                      auto_track=live.auto_track)
    outcome = ScanOutcome(day=day, scanned=len(histories), candidates=rows,
                          skipped=max(0, len(candidates) - len(rows)))
    if not commit:
        return outcome

    # Give the tracker a trading calendar even before a full sync has run.
    reference = max(histories.values(), key=len)
    db.upsert_days(d.date() for d in reference.index[-500:])

    auto_track = live.auto_track
    for row in rows:
        signal_id = db.insert_signal(row)
        if signal_id is None:
            continue                      # already stored for this date
        outcome.stored += 1
        if auto_track:
            db.insert_trade({"signal_id": signal_id, "symbol": row["symbol"],
                             "exchange": row["exchange"], "status": "suggested",
                             "signal_date": day})
            outcome.tracked += 1
    db.upsert_days([day])
    log.info("scan %s: %d candidates, %d new, %d auto-tracked",
             day, len(rows), outcome.stored, outcome.tracked)
    return outcome


def _as_date(value: Date | str | None) -> Date | None:
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    return value
