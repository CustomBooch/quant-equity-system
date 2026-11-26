"""PAPER schedule and tranche targets (lab/paper/SPEC.md sections 1-4). Pure functions; nothing here sends or reads an account.

A slot is a Tuesday. Tranche i (0..K-1) is rebuilt every K weeks, on a continuous week count (A33; the ISO week number of A5 until 2026).
Each tranche holds 1/K of the capital, times the overlay exposure, in the monthly target weights; the trade of a slot is the difference between that tranche's new and old shares.
"""
from __future__ import annotations

import datetime as dt
import math

K_TRANCHES = 4
WEEKDAY = 1                                                                     # Tuesday
DEFENSIVE_EXPOSURE = 0.5                                                        # SPEC amendment A3 (owner: low drawdown, high hit rate); shadow arms 0.75 and 0.25
SHADOW_LEVELS = (0.75, 0.25)


def is_slot(day: dt.date) -> bool:
    return day.weekday() == WEEKDAY


def tranche_of(day: dt.date, k: int = K_TRANCHES) -> int:
    """Weeks counted without a break (A33): the ISO week number (A5) restarts at 1 after a 53-week year, so 2026-12-29 and 2027-01-05 would both be tranche 1.
    The offset 2 gives every 2026 slot the tranche the ISO rule gave it."""
    if not is_slot(day):
        raise ValueError(f"{day} is not a Tuesday slot")
    return (day.toordinal() // 7 + 2) % k


# One-time exceptions registered by amendment: on these slots the listed tranches may be planned in addition to the slot's own (the A5 calendar
# resumes the week after). A29/A30: the whole account moves to the model on 2026-11-24.
CATCH_UP: dict[dt.date, tuple[int, ...]] = {dt.date(2026, 11, 24): (0, 1, 2, 3)}


def plannable(day: dt.date, tranche: int, k: int = K_TRANCHES) -> bool:
    """Whether `tranche` may be planned on slot `day`: its own tranche, or one an amendment listed in CATCH_UP for that day."""
    return tranche == tranche_of(day, k) or tranche in CATCH_UP.get(day, ())


def overlay_exposure(market_close: float, ten_month_mean: float, level: float = DEFENSIVE_EXPOSURE) -> float:
    """Binary trend overlay: `level` exposure (0.5 by default) when the total-market series is below its 10-month mean, else full."""
    if not (math.isfinite(market_close) and math.isfinite(ten_month_mean)):
        raise ValueError(f"overlay inputs must be finite (close {market_close}, 10-month mean {ten_month_mean}): the trend history is incomplete")
    return level if market_close < ten_month_mean else 1.0


def tranche_target_shares(weights: dict[str, float], prices: dict[str, float], account_value_usd: float, exposure: float, k: int = K_TRANCHES) -> dict[str, float]:
    """Target shares of one tranche: weights are the book weights (sum to 1), prices in USD."""
    budget = account_value_usd / k * exposure
    return {t: round(w * budget / prices[t], 4) for t, w in weights.items() if t in prices and prices[t] > 0}
