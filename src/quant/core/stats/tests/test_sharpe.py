"""quant.core.stats.sharpe: more trials raise the luck benchmark and lower the deflated probability, a series too short or flat is refused, and the result
equals the research copy (lab.stageb.deflated_sharpe)."""
import numpy as np
import pandas as pd
import pytest

from quant.core.stats import sharpe as SH


def series(mean=0.01, sd=0.04, n=60, seed=1):
    return np.random.default_rng(seed).normal(mean, sd, n)


def test_more_trials_raise_the_benchmark_and_lower_the_probability():
    r = series()
    a, b = SH.deflated_sharpe(r, 10), SH.deflated_sharpe(r, 20_000)
    assert b["sr_benchmark_max_of_trials"] > a["sr_benchmark_max_of_trials"] and b["dsr_prob"] < a["dsr_prob"]
    assert a["monthly_sharpe"] == pytest.approx(b["monthly_sharpe"]) and a["annualised_sharpe"] == pytest.approx(a["monthly_sharpe"] * 12**0.5)


def test_one_trial_has_no_luck_benchmark_so_a_positive_series_scores_high_and_a_negative_one_low():
    assert SH.expected_max_sharpe(1, 60) == 0.0
    assert SH.deflated_sharpe(series(0.02), 1)["dsr_prob"] > 0.95 and SH.deflated_sharpe(series(-0.02), 1)["dsr_prob"] < 0.05


def test_a_series_under_twelve_months_or_without_variation_is_refused_and_nans_are_dropped():
    with pytest.raises(ValueError, match="at least 12 months"):
        SH.deflated_sharpe(series(n=11), 100)
    with pytest.raises(ValueError, match="at least 12 months"):
        SH.deflated_sharpe(np.full(30, 0.01), 100)
    assert SH.deflated_sharpe(np.append(series(), np.nan), 100)["months"] == 60


def test_the_result_equals_the_research_copy_in_lab_stageb():
    stageb = pytest.importorskip("lab.stageb")
    r = series(0.012, 0.05, 80, seed=7)
    ref = stageb.deflated_sharpe(pd.Series(r), 5000)
    got = SH.deflated_sharpe(r, 5000)
    assert got["monthly_sharpe"] == pytest.approx(ref["monthly_sharpe"]) and got["sr_benchmark_max_of_trials"] == pytest.approx(ref["sr_benchmark_max_of_trials"])
    assert got["dsr_prob"] == pytest.approx(ref["dsr_prob"])
