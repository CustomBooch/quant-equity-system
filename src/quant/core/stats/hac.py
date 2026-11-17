"""Newey-West (Bartlett kernel) inference for the mean of a series.

Convention (matches every existing definition in the repository): the long-run variance is s = g0 + 2 * sum_k (1 - k/(L+1)) * g_k with g_k = u[k:] @ u[:-k] / n, the standard
error of the mean is sqrt(s / n), no small-sample correction, and fewer than `min_obs` observations gives NaN. The p-value helper uses the normal distribution.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import norm

DEFAULT_LAGS = 6
MIN_OBS = 12


def nw_mean_t(x: npt.ArrayLike, lags: int = DEFAULT_LAGS, min_obs: int = MIN_OBS) -> tuple[float, float, int]:
    """Mean, Newey-West t of the mean, and n, after dropping NaN. Lags are capped at n - 1."""
    v = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(v)
    if n < min_obs:
        return float("nan"), float("nan"), n
    m = v.mean()
    u = v - m
    s = u @ u / n
    for k in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - k / (lags + 1)) * (u[k:] @ u[:-k]) / n
    return float(m), float(m / np.sqrt(s / n)) if s > 0 else float("nan"), n


def nw_t(x: npt.ArrayLike, lags: int = DEFAULT_LAGS) -> float:
    """Newey-West t of the mean (same value as lab.run.nw_t)."""
    return nw_mean_t(x, lags)[1]


def nw_t_matrix(Y: np.ndarray, lags: int = DEFAULT_LAGS) -> np.ndarray:
    """Newey-West t of the mean for every column of Y (T x n) at once; no NaN handling, no minimum length (used for Monte-Carlo nulls)."""
    T = Y.shape[0]
    U = Y - Y.mean(0)
    s = (U * U).sum(0) / T
    for k in range(1, lags + 1):
        s += 2 * (1 - k / (lags + 1)) * (U[k:] * U[:-k]).sum(0) / T
    t: np.ndarray = Y.mean(0) / np.sqrt(s / T)
    return t


def nw_p_normal(t: float, one_sided: bool = False) -> float:
    """Normal-approximation p-value of a t-statistic; one-sided tests the upper tail."""
    if not np.isfinite(t):
        return float("nan")
    return float(1 - norm.cdf(t)) if one_sided else float(2 * (1 - norm.cdf(abs(t))))


def hac_mean_ols(y: Sequence[float] | np.ndarray | pd.Series, X: npt.ArrayLike | None = None, lags: int = DEFAULT_LAGS) -> Any:
    """statsmodels OLS of y on a constant (plus X) with Bartlett HAC errors, for regressions where the hand-written mean test is not enough.
    Returns the fitted statsmodels results object (statsmodels ships no type information)."""
    import statsmodels.api as sm

    exog = np.ones((len(y), 1)) if X is None else sm.add_constant(X)
    return sm.OLS(np.asarray(y, float), exog).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
