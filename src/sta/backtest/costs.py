"""Transaction cost model for Indian equity delivery trades.

Defaults approximate a discount broker (Zerodha-style) on NSE delivery:

- brokerage: flat ₹20 per executed order (Zerodha charges ₹0 for delivery;
  keep a configurable flat fee so other brokers/costs can be modelled)
- STT: 0.1% on delivery, charged on both buy and sell side
- exchange transaction charge: ~0.00297% (NSE equity)
- SEBI turnover fee: 0.0001%
- stamp duty: 0.015% buy side only
- GST: 18% on (brokerage + exchange + SEBI charges)

Everything is expressed in rupees and applied per fill, so the engine
stays broker-agnostic: only ``fill_cost`` changes per model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0
    stt_pct: float = 0.001          # both sides, delivery
    exchange_pct: float = 0.0000297
    sebi_pct: float = 0.000001
    stamp_pct: float = 0.00015      # buy side only
    gst_pct: float = 0.18           # on brokerage + exchange + sebi

    def buy_cost(self, value: float) -> float:
        statutory = value * (self.stt_pct + self.exchange_pct + self.sebi_pct + self.stamp_pct)
        return self.brokerage_per_order + statutory + self.gst_pct * (
            self.brokerage_per_order + value * (self.exchange_pct + self.sebi_pct))

    def sell_cost(self, value: float) -> float:
        statutory = value * (self.stt_pct + self.exchange_pct + self.sebi_pct)
        return self.brokerage_per_order + statutory + self.gst_pct * (
            self.brokerage_per_order + value * (self.exchange_pct + self.sebi_pct))

    def round_trip_bps(self, value: float) -> float:
        """Reference number for reporting: costs as bps of traded value."""
        return 10_000 * (self.buy_cost(value) + self.sell_cost(value)) / max(value, 1e-9)
