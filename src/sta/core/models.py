"""Core domain models.

Pure dataclasses with no I/O and no framework dependencies, so every layer
(scanners, backtester, reports) can depend on them safely.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Optional


class Exchange(str, enum.Enum):
    NSE = "NSE"
    BSE = "BSE"


@dataclass(frozen=True)
class Bar:
    """One normalized trading day for one instrument.

    ``symbol`` is the canonical (exchange-native) trading symbol, e.g.
    ``RELIANCE`` on NSE or a numeric code on BSE. ``isin`` is the shared
    company identity across exchanges.
    """

    symbol: str
    exchange: Exchange
    isin: str
    date: Date
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    volume: int
    value: float  # traded turnover in rupees

    @property
    def gap(self) -> float:
        """Overnight change from previous close to this bar's open."""
        if self.prev_close <= 0:
            return 0.0
        return self.open / self.prev_close - 1.0


@dataclass(frozen=True)
class SymbolMeta:
    """Static-ish metadata for one tradable instrument."""

    symbol: str
    exchange: Exchange
    isin: str
    name: str
    series: str  # NSE series (EQ/BE/...) or BSE group (A/B/T/...)
    first_date: Optional[Date] = None
    last_date: Optional[Date] = None


@dataclass(frozen=True)
class UniverseMembership:
    """A symbol's admission into (and exit from) the tradeable universe."""

    symbol: str
    exchange: Exchange
    isin: str
    start: Date
    end: Date  # inclusive; membership expires after this date


@dataclass(frozen=True)
class CorporateAction:
    """A detected capital-structure event affecting history continuity."""

    symbol: str
    exchange: Exchange
    isin: str
    date: Date
    kind: str  # "split", "bonus", "unverified_gap"
    ratio: float  # shares multiply by this on the event date


@dataclass
class ScanCandidate:
    """Output of the scanner layer for a single day."""

    symbol: str
    exchange: Exchange
    isin: str
    name: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    close: float = 0.0
    volume: int = 0
    turnover: float = 0.0
