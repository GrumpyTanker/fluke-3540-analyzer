"""TOU tariff calculator — translate Wh_total into $$ cost.

Supports a simple peak/off-peak schedule. Peak hours are a list of
``(start_hour, end_hour)`` pairs in 24-hour wall time UTC (we follow the
record timestamps, which are UTC). Any record outside the peak windows
is billed at the off-peak rate.

Future: per-weekday schedules, holidays, tiered demand charges. Out of
scope for v0.4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .parser import FIELDS, Record


_FIELD_INDEX = {f.name: f.index for f in FIELDS}


@dataclass(frozen=True)
class Tariff:
    currency: str = "USD"
    peak_rate: float = 0.0        # $/kWh
    offpeak_rate: float = 0.0     # $/kWh
    peak_hours: tuple[tuple[int, int], ...] = ()  # ((start_h, end_h),)
    # end_h is exclusive; a value < start_h means the window wraps midnight.

    def is_peak(self, hour: int) -> bool:
        for start, end in self.peak_hours:
            if start == end:
                continue
            if start < end:
                if start <= hour < end:
                    return True
            else:  # wraps midnight
                if hour >= start or hour < end:
                    return True
        return False


def compute_cost(records: Iterable[Record], tariff: Tariff) -> dict:
    """Walk records, bucket Wh_total by peak/off-peak, return cost breakdown.

    Returns a dict with:
      currency, peak_kwh, offpeak_kwh, peak_cost, offpeak_cost,
      imported_cost, exported_cost, net_cost,
      peak_imported_kwh, peak_exported_kwh,
      offpeak_imported_kwh, offpeak_exported_kwh
    """
    if "Wh_total" not in _FIELD_INDEX:
        return _empty(tariff)
    wh_idx = _FIELD_INDEX["Wh_total"]
    pk_imp = pk_exp = op_imp = op_exp = 0.0
    for r in records:
        wh = r.floats[wh_idx]
        if not wh:
            continue
        hour = r.start.hour
        is_peak = tariff.is_peak(hour)
        if wh > 0:
            if is_peak:
                pk_imp += wh
            else:
                op_imp += wh
        else:
            if is_peak:
                pk_exp += wh
            else:
                op_exp += wh
    pk_imp_k = pk_imp / 1000.0
    pk_exp_k = pk_exp / 1000.0
    op_imp_k = op_imp / 1000.0
    op_exp_k = op_exp / 1000.0
    peak_imp_cost = pk_imp_k * tariff.peak_rate
    peak_exp_cost = pk_exp_k * tariff.peak_rate
    off_imp_cost = op_imp_k * tariff.offpeak_rate
    off_exp_cost = op_exp_k * tariff.offpeak_rate
    imported_cost = peak_imp_cost + off_imp_cost
    exported_cost = peak_exp_cost + off_exp_cost
    return {
        "currency": tariff.currency,
        "peak_kwh":             pk_imp_k + pk_exp_k,
        "offpeak_kwh":          op_imp_k + op_exp_k,
        "peak_imported_kwh":    pk_imp_k,
        "peak_exported_kwh":    pk_exp_k,
        "offpeak_imported_kwh": op_imp_k,
        "offpeak_exported_kwh": op_exp_k,
        "peak_cost":            peak_imp_cost + peak_exp_cost,
        "offpeak_cost":         off_imp_cost + off_exp_cost,
        "imported_cost":        imported_cost,
        "exported_cost":        exported_cost,
        "net_cost":             imported_cost + exported_cost,
    }


def _empty(tariff: Tariff) -> dict:
    keys = (
        "peak_kwh offpeak_kwh peak_imported_kwh peak_exported_kwh "
        "offpeak_imported_kwh offpeak_exported_kwh peak_cost offpeak_cost "
        "imported_cost exported_cost net_cost"
    ).split()
    return {"currency": tariff.currency, **{k: 0.0 for k in keys}}
