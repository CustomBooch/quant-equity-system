"""Multiple-testing and cross-sectional helpers that exist in several slightly different copies across the repository (new code uses these).

The copies differ in convention, and the differences are options here rather than accidents:
  * threshold comparison: lab/n3a, lab/prospective/a1 and newer code reject when p < alpha/(m-i); legacy e6_returns / signal_batch_02 use p <= alpha/(m-i)      -> `strict`
  * a missing p-value: e6_returns counts it as a failure (p = 1)                                                                                                   -> `None` -> 1.0
  * lab/c5 returns Holm-adjusted p-values instead of decisions                                                                                                      -> `holm_adjusted`
"""
from __future__ import annotations

from typing import TypedDict

import pandas as pd


class HolmStep(TypedDict):
    """One hypothesis of a Holm step-down: its p-value (a missing one counts as 1), the threshold alpha/(m-i) it was compared with, and the decision."""
    p: float
    threshold: float
    reject: bool


def holm_steps(ps: dict[str, float | None], alpha: float = 0.05, strict: bool = True) -> dict[str, HolmStep]:
    """Holm step-down: sort ascending, compare the i-th smallest with alpha/(m-i), stop rejecting at the first failure. Returns p, threshold and reject per name."""
    items = sorted(((k, 1.0 if v is None else float(v)) for k, v in ps.items()), key=lambda kv: kv[1])
    m, passed = len(items), True
    out: dict[str, HolmStep] = {}
    for i, (name, p) in enumerate(items):
        thr = alpha / (m - i)
        passed = passed and (p < thr if strict else p <= thr)
        out[name] = {"p": p, "threshold": thr, "reject": bool(passed)}
    return out


def holm_reject(ps: dict[str, float | None], alpha: float = 0.05, strict: bool = True) -> dict[str, bool]:
    """The Holm decisions of holm_steps, by name."""
    return {k: v["reject"] for k, v in holm_steps(ps, alpha, strict).items()}


def holm_adjusted(p: dict[str, float]) -> dict[str, float]:
    """Holm-adjusted p-values (running maximum of (m-i)*p, capped at 1)."""
    items = sorted(p.items(), key=lambda kv: kv[1])
    m, run = len(items), 0.0
    out: dict[str, float] = {}
    for i, (k, v) in enumerate(items):
        run = max(run, min(1.0, (m - i) * v))
        out[k] = run
    return out


def crank(s: pd.Series, g: pd.Series) -> pd.Series:
    """Within-group centred rank in (-0.5, 0.5): (average rank - 0.5) / count - 0.5 (the lab.run convention). NaN stay NaN."""
    r = s.groupby(g)
    return (r.rank(method="average") - 0.5) / r.transform("count") - 0.5


def monthly_rank_ic(d: pd.DataFrame, a: str, b: str, min_n: int = 100) -> pd.Series:
    """Spearman rank correlation of columns a and b within each month; months with fewer than min_n complete rows are skipped."""
    out = {}
    for m, month_rows in d.groupby("month"):
        g = month_rows[[a, b]].dropna()
        if len(g) >= min_n:
            out[m] = g[a].rank().corr(g[b].rank())
    return pd.Series(out, dtype=float).sort_index()


def holm_rejections_in_order(ps: list[float | None], alpha: float = 0.05) -> list[bool]:
    """Positional variant used by lab/prospective/a1.py: decisions in the input order."""
    r = holm_reject({str(i): p for i, p in enumerate(ps)}, alpha)
    return [r[str(i)] for i in range(len(ps))]


__all__ = ["crank", "holm_adjusted", "holm_reject", "holm_rejections_in_order", "holm_steps", "monthly_rank_ic"]
