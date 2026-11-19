"""Data pipeline orchestration: market-cap universe -> per-ticker download.

Each step is explicit and resumable, mirroring the reference pipeline's
cache-skip behavior:

1. ``build_universe`` — market-cap scan over ``Equity.csv`` tickers
   (cached in ``data/universe/market_caps.parquet``), filtered to
   micro/small/mid caps <= ``max_mcap_cr``.
2. ``download`` — ``period="max"`` daily bars per ticker into
   ``data/daily/<TICKER>.parquet``; existing files are skipped unless
   ``force=True``.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from ..core.config import Settings
from .fetch import download_ticker
from .universe import build_universe, fetch_market_caps, load_tickers

log = logging.getLogger(__name__)


class DataPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.universe_dir = settings.universe_dir
        self.universe_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ universe

    def build_universe(self, workers: int = 12, sleep: float = 0.05) -> pd.DataFrame:
        cfg = self.settings.universe
        tickers = load_tickers(self.settings.root / cfg.tickers_file)
        caps = fetch_market_caps(tickers,
                                 cache_path=self.universe_dir / "market_caps.parquet",
                                 workers=workers, sleep=sleep)
        membership = build_universe(tickers, caps, cfg)
        out_path = self.universe_dir / "membership.parquet"
        membership.to_parquet(out_path, index=False)
        log.info("membership written: %s (%d symbols)", out_path, len(membership))
        return membership

    # ------------------------------------------------------------ download

    def download(self, symbols: list[str], workers: int = 8,
                 force: bool = False) -> dict[str, int]:
        """Download max-history parquet for each symbol (resumable)."""
        dl = self.settings.download
        stats = {"ok": 0, "cached": 0, "failed": 0}
        failed: list[str] = []
        done = 0

        def _one(ticker: str):
            path = Path(fetch_download_dir()) / f"{ticker.replace(':', '_')}.parquet"
            if path.exists() and not force and not _is_stub(path):
                return "cached"
            download_ticker(ticker, period=dl.period, interval=dl.interval,
                            force=force, sleep=dl.sleep)
            return "ok"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_one, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                done += 1
                try:
                    stats[fut.result()] += 1
                except Exception as exc:  # noqa: BLE001 — one bad ticker must not stop the run
                    stats["failed"] += 1
                    failed.append(f"{sym}: {exc}")
                if done % 250 == 0:
                    log.info("download progress %d / %d: %s", done, len(symbols), stats)

        if failed:
            detail = self.universe_dir / "download_failed.csv"
            pd.DataFrame({"error": failed}).to_csv(detail, index=False)
            log.warning("%d failures written to %s", len(failed), detail)
        log.info("download finished: %s", stats)
        return stats


def fetch_download_dir() -> Path:
    from .fetch import DATA_DIR
    return DATA_DIR


def _is_stub(path: Path) -> bool:
    """True when a cached history is suspiciously short (Yahoo glitch)."""
    import pyarrow.parquet as pq
    rows = pq.read_metadata(path).num_rows
    return rows < 30
