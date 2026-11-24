"""Capped portfolio weights: the rule every live book uses (the long book's 5% cap, the short basket's 5% cap, the shadow arms), and the SIC-division
sector cap of the long book from decision month 2026-12 (PAPER amendment A22; copies of lab.portfolio.policy's functions, tied by the same parity test).

The algorithm is the one the research recorded results were computed with (`lab.core.econ.capped_weights`, itself a byte-identical copy of the frozen
`legacy_research/signal_batch_03/econ.py`); tools/tests/test_duplicate_parity.py checks that the three give identical arrays. The production path imports it from
here, so it does not depend on the research record (tools/check_imports.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

CAP = 0.10


def capped_weights(me: np.ndarray, cap: float = CAP) -> np.ndarray:
    """Weights proportional to `me`, none above `cap`: the excess of capped names is handed to the uncapped ones in proportion to their weight, repeatedly (at
    most 100 rounds). When fewer than 1/cap names exist no feasible answer exists and every name ends at or near the cap; callers pass max(cap, 1/n)."""
    w = me / me.sum()
    for _ in range(100):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        free = ~over & (w < cap)
        if not free.any():
            break
        w[free] += excess * w[free] / w[free].sum()
    out: np.ndarray = w
    return out


def sic_division(sic2: pd.Series) -> np.ndarray:
    """SIC division index (0-9) of each two-digit SIC code; -1 for a missing or unlisted code."""
    s = sic2.fillna(-1).astype(int).to_numpy()
    edges = [(1, 9), (10, 14), (15, 17), (20, 39), (40, 49), (50, 51), (52, 59), (60, 67), (70, 89), (90, 99)]
    out = np.full(len(s), -1)
    for k, (lo, hi) in enumerate(edges):
        out[(s >= lo) & (s <= hi)] = k
    return out


def _fill(w: np.ndarray, free: np.ndarray, amount: float, cap: float) -> float:
    """Water-fill `amount` into names `free` pro rata to their weights without exceeding `cap`; returns what could not be placed."""
    for _ in range(100):
        room = np.where(free, np.maximum(cap - w, 0.0), 0.0)
        live = free & (room > 1e-15)
        if amount <= 1e-15 or not live.any():
            break
        share = w[live] / w[live].sum() if w[live].sum() > 0 else np.full(live.sum(), 1.0 / live.sum())
        add = np.minimum(amount * share, room[live])
        w[live] += add
        amount -= add.sum()
    return amount


def apply_sector_cap(w: np.ndarray, sec: np.ndarray, u_sec: Mapping[Any, float], extra: float, cap: float) -> np.ndarray:
    """Each sector <= universe weight + extra and each name <= cap, jointly; if infeasible the remainder stays where it was."""
    w = w.copy()
    cap = max(cap, 1.0 / len(w))
    for _ in range(50):
        lim = {s: u_sec.get(s, 0.0) + extra for s in np.unique(sec)}
        over = [s for s in lim if w[sec == s].sum() > lim[s] + 1e-12]
        if not over:
            break
        at_limit = np.isin(sec, [s for s in lim if w[sec == s].sum() >= lim[s] - 1e-12])
        moved = 0.0
        for s in over:
            m = sec == s
            exc = w[m].sum() - lim[s]
            w[m] *= lim[s] / w[m].sum()
            left = _fill(w, ~at_limit, exc, cap)
            w[m] += left * w[m] / w[m].sum()                                            # unplaceable remainder returns: infeasible case
            moved += exc - left
        if moved <= 1e-12:
            break
    return w
