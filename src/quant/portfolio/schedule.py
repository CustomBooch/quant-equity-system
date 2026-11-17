"""PAPER schedule and tranche targets (lab/paper/SPEC.md sections 1-4). Pure functions; nothing here sends or reads an account.

A slot is a Tuesday. Tranche i (0..K-1) is rebuilt on the Tuesdays whose ISO week number mod K equals i, so each tranche is rebuilt once every K weeks.
Each tranche holds 1/K of the capital, times the overlay exposure, in the monthly target weights; the trade of a slot is the difference between that tranche's new and old shares.
"""
from __future__ import annotations

import datetime as dt

K_TRANCHES = 4
WEEKDAY = 1                                                                     # Tuesday
DEFENSIVE_EXPOSURE = 0.5                                                        # SPEC amendment A3 (owner: low drawdown, high hit rate); shadow arms 0.75 and 0.25
SHADOW_LEVELS = (0.75, 0.25)


def is_slot(day: dt.date) -> bool:
    return day.weekday() == WEEKDAY


def tranche_of(day: dt.date, k: int = K_TRANCHES) -> int:
    if not is_slot(day):
        raise ValueError(f"{day} is not a Tuesday slot")
    return day.isocalendar().week % k


def overlay_exposure(market_close: float, ten_month_mean: float, level: float = DEFENSIVE_EXPOSURE) -> float:
    """Binary trend overlay: `level` exposure (0.5 by default) when the total-market series is below its 10-month mean, else full."""
    return level if market_close < ten_month_mean else 1.0


def tranche_target_shares(weights: dict[str, float], prices: dict[str, float], account_value_usd: float, exposure: float, k: int = K_TRANCHES) -> dict[str, float]:
    """Target shares of one tranche: weights are the book weights (sum to 1), prices in USD."""
    budget = account_value_usd / k * exposure
    return {t: round(w * budget / prices[t], 4) for t, w in weights.items() if t in prices and prices[t] > 0}
