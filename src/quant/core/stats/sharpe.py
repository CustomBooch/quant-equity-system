"""Deflated Sharpe ratio (Bailey and Lopez de Prado): how likely a monthly series' Sharpe ratio is above what the best of `trials` zero-skill strategies would
show by luck. The same formula as `lab.stageb.deflated_sharpe` (core/stats/tests/test_sharpe.py ties them); trials come from the experiment registry
(`quant.core.registry.query.trial_count`), so the number deflated against is the number of configurations actually tried."""
from __future__ import annotations

import math
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import norm

EULER = 0.5772156649


def expected_max_sharpe(trials: int, n: int) -> float:
    """Monthly Sharpe the best of `trials` zero-skill strategies is expected to reach over `n` months."""
    if trials < 2:
        return 0.0
    return math.sqrt(1 / n) * ((1 - EULER) * float(norm.ppf(1 - 1 / trials)) + EULER * float(norm.ppf(1 - 1 / (trials * math.e))))


def deflated_sharpe(returns: npt.ArrayLike, trials: int) -> dict[str, Any]:
    """{months, monthly_sharpe, annualised_sharpe, sr_benchmark_max_of_trials, dsr_prob (the probability the true Sharpe exceeds the luck benchmark), trials}.
    NaNs are dropped; fewer than 12 months, or no variation, raises ValueError because the skew and kurtosis terms are meaningless."""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    n = len(r)
    if n < 12 or float(r.std(ddof=1)) < 1e-12:
        raise ValueError(f"a deflated Sharpe needs at least 12 months with variation (got {n})")
    sd = float(r.std(ddof=1))
    sr = float(r.mean()) / sd
    s = pd.Series(r)
    skew, kurt = cast(float, s.skew()), cast(float, s.kurt()) + 3                                                       # unbiased sample skew; kurtosis, not excess
    sr0 = expected_max_sharpe(trials, n)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    stat = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return {"months": n, "trials": trials, "monthly_sharpe": sr, "annualised_sharpe": sr * math.sqrt(12), "sr_benchmark_max_of_trials": sr0,
            "dsr_prob": float(norm.cdf(stat))}
