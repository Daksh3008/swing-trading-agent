"""Momentum scanner: per-symbol features -> cross-sectionally ranked
daily candidate list.

Signature of a candidate (all must hold):

- inside the tradeable universe that day
- 52-week-high proximity <= ``high_proximity`` (breakout style)
- 3-month return percentile in the top ``rs_pct`` of the universe
- volume surge >= ``vol_surge``
- trend structure intact (price > rising MA50 > rising MA150)

Score = weighted rank blend; only the top ``max_candidates`` per day are
emitted. Pure function of the feature matrix -> trivially backtestable.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date as Date

import numpy as np
import pandas as pd

from ..core.models import Exchange, ScanCandidate
from .features import RMA_LENGTHS, compute_features

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScannerParams:
    high_proximity: float = -0.05       # within 5% of 52w high
    rs_pct: float = 0.85                # top 15% 3-month RS
    vol_surge: float = 2.0              # 2x 20d average volume
    max_candidates: int = 10
    trend_mode: str = "sma"             # "sma" | "kama" | "rma"
    rma_fast: int = 25                  # RMA gate lengths (trend_mode="rma")
    rma_slow: int = 75
    weights: tuple[tuple[str, float], ...] = (
        ("rs_rank", 0.40),
        ("high_rank", 0.30),   # closer to high = higher
        ("vol_rank", 0.30),
    )


class Scanner(ABC):
    @abstractmethod
    def scan(self, day: Date) -> list[ScanCandidate]: ...


class MomentumScanner(Scanner):
    def __init__(self, histories: dict[str, pd.DataFrame],
                 membership: pd.DataFrame,
                 params: ScannerParams | None = None):
        """``histories`` maps ``SYMBOL`` -> raw history (date-indexed).
        ``membership`` is the universe span frame."""
        self.params = params or ScannerParams()
        self._spans = self._index_spans(membership)
        self._by_day = self._build_feature_days(histories)

    # ------------------------------------------------------------------ scan

    def scan(self, day: Date) -> list[ScanCandidate]:
        day = _as_day(day)
        feats = self._by_day.get(str(day))
        if feats is None or feats.empty:
            return []
        feats = feats[self._member_mask(feats, day)]
        if feats.empty:
            return []
        scored = self._score(feats).sort_values("score", ascending=False)
        return self._to_candidates(scored.head(self.params.max_candidates))

    # ------------------------------------------------------------------ core

    @staticmethod
    def _index_spans(membership: pd.DataFrame) -> dict[tuple[str, str], list[tuple]]:
        spans: dict[tuple[str, str], list[tuple]] = {}
        if membership is None or membership.empty:
            return spans
        for row in membership.itertuples(index=False):
            spans.setdefault((row.exchange, row.symbol), []).append(
                (_as_date(row.start), _as_date(row.end)))
        return spans

    @staticmethod
    def _build_feature_days(histories: dict[str, pd.DataFrame]) -> dict[Date, pd.DataFrame]:
        """Group the full feature frame once by date -> O(1) per scan day."""
        frames = []
        for symbol, hist in histories.items():
            feats = compute_features(hist)
            feats["symbol"] = symbol
            feats["close"] = hist["close"].values
            feats["volume"] = hist["volume"].values
            feats["name"] = hist["name"].iloc[0]
            frames.append(feats)
        if not frames:
            return {}
        out = pd.concat(frames)
        out = out.reset_index()
        out = out.rename(columns={out.columns[0]: "date"})
        out["date"] = pd.to_datetime(out["date"]).dt.date
        return {str(day): frame for day, frame in out.groupby("date")}

    def _member_mask(self, feats: pd.DataFrame, day: Date) -> np.ndarray:
        return np.array([
            self._member(str(row.exchange), str(row.symbol), day)
            for row in feats.itertuples(index=False)
        ])

    def _member(self, exchange: str, symbol: str, day: Date) -> bool:
        for start, end in self._spans.get((exchange, symbol), ()):
            if start <= day <= end:
                return True
        return False

    def _score(self, feats: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = feats.copy()
        out["rs_rank"] = _pct_rank(out["ret_3m"])
        out["high_rank"] = _pct_rank(-out["pct_from_high"])
        out["vol_rank"] = _pct_rank(out["vol_ratio"])
        out["score"] = sum(w * out[k].fillna(0) for k, w in p.weights)

        if p.trend_mode == "rma":
            fast_col = f"rma_{p.rma_fast}"
            slow_col = f"rma_{p.rma_slow}"
            if fast_col not in out.columns or slow_col not in out.columns:
                raise ValueError(
                    f"RMA lengths must be in {RMA_LENGTHS}: "
                    f"got fast={p.rma_fast} slow={p.rma_slow}")
            trend_ok = (
                (out["close"] > out[fast_col])
                & out[f"{fast_col}_up"].astype(bool)
                & (out[fast_col] > out[slow_col])
                & out[f"{slow_col}_up"].astype(bool)
            )
        else:
            trend_col = "trend_ok_kama" if p.trend_mode == "kama" else "trend_ok"
            trend_ok = out[trend_col].astype(bool)
        gate = (
            (out["pct_from_high"] >= p.high_proximity)
            & (out["rs_rank"] >= p.rs_pct)
            & (out["vol_ratio"] >= p.vol_surge)
            & trend_ok
        )
        return out[gate]

    def _to_candidates(self, scored: pd.DataFrame) -> list[ScanCandidate]:
        return [
            ScanCandidate(
                symbol=row.symbol,
                exchange=Exchange(row.exchange),
                isin=row.isin,
                name=str(row.name),
                score=float(row.score),
                close=float(row.close),
                volume=int(row.volume),
            )
            for row in scored.itertuples(index=False)
        ]


def _as_day(value) -> Date:
    """Normalize Timestamp/python-date inputs to a plain date."""
    return value.date() if isinstance(value, pd.Timestamp) else value


def _pct_rank(series: pd.Series) -> pd.Series:
    """Percentile rank within the day's cross-section (0..1)."""
    return series.rank(pct=True, na_option="bottom")


def _as_date(value) -> Date:
    return value.date() if hasattr(value, "date") else value
