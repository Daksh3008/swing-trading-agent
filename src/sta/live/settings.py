"""Runtime-editable live settings (TOML defaults + ``app_state`` overrides)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from ..core.config import Settings
from .rules import RuleParams

_STATE_KEY = "live.settings"


@dataclass(frozen=True)
class LiveSettings:
    position_size: float
    max_positions: int
    auto_track: bool
    scan_top_n: int
    stop_pct: float
    target_pct: float
    trailing_atr_mult: float
    atr_window: int

    def rules(self) -> RuleParams:
        """The validated strategy: trailing 3xATR(22), no fixed target."""
        return RuleParams(
            position_size=self.position_size,
            max_positions=self.max_positions,
            stop_pct=self.stop_pct,
            target_pct=self.target_pct,
            trailing=True,
            trailing_atr_mult=self.trailing_atr_mult,
            atr_window=self.atr_window,
            let_winners_run=True,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_live_settings(settings: Settings, db) -> LiveSettings:
    cfg = settings.live
    base = LiveSettings(
        position_size=cfg.position_size,
        max_positions=cfg.max_positions,
        auto_track=cfg.auto_track,
        scan_top_n=cfg.scan_top_n,
        stop_pct=cfg.stop_pct,
        target_pct=cfg.target_pct,
        trailing_atr_mult=cfg.trailing_atr_mult,
        atr_window=cfg.atr_window,
    )
    overrides = db.get_json(_STATE_KEY, {}) or {}
    allowed = set(base.as_dict())
    clean = {k: v for k, v in overrides.items() if k in allowed}
    return replace(base, **clean) if clean else base


def save_live_settings(db, **changes: Any) -> dict[str, Any]:
    current = db.get_json(_STATE_KEY, {}) or {}
    current.update({k: v for k, v in changes.items() if v is not None})
    db.set_json(_STATE_KEY, current)
    return current
