"""quant.features.sources.monthly_asof: for decision month M, each stock's newest value usable by the last day of M - 1; never a value published later,
old values dropped after max_age_days, tickers mapped to permnos through the link table in force when the value became usable."""
from __future__ import annotations

import pandas as pd
import pytest

from quant.data.sources.base import Dataset, conform
from quant.features.sources import monthly_asof

DS = Dataset(name="toy", licence="Data source framework", entity="ticker", observed="period_end", known="published", columns={"sales": "float64"})
LINK = pd.DataFrame({"id": ["AAA", "BBB", "BBB"], "permno": [1, 2, 3], "start": ["2000-01-01", "2000-01-01", "2026-09-01"], "end": [None, "2026-08-31", None]})


def rows():
    return conform(pd.DataFrame({"ticker": ["AAA", "AAA", "BBB", "BBB"], "period_end": ["2026-03-31", "2026-06-30", "2026-06-30", "2026-09-30"],
                                 "published": ["2026-04-30", "2026-07-31", "2026-08-10", "2026-10-20"], "sales": [1.0, 2.0, 5.0, 6.0]}), DS)


def test_a_value_is_used_from_the_month_after_it_was_published_and_never_before():
    f = monthly_asof(rows(), DS, pd.to_datetime(["2026-07-01", "2026-08-01", "2026-09-01", "2026-11-01"]), link=LINK)
    got = {(str(m.date()), p): v for m, p, v in f[["month", "permno", "toy_sales"]].itertuples(index=False)}
    assert got == {("2026-07-01", 1): 1.0, ("2026-08-01", 1): 2.0,              # AAA's Q2, published 2026-07-31, is first used for August
                   ("2026-09-01", 1): 2.0, ("2026-09-01", 2): 5.0,              # BBB published 2026-08-10, when the ticker named permno 2
                   ("2026-11-01", 1): 2.0, ("2026-11-01", 2): 5.0, ("2026-11-01", 3): 6.0}


def test_the_lag_delays_use_and_an_old_value_is_dropped_after_max_age():
    lagged = Dataset(**{**DS.__dict__, "lag_days": 5})
    f = monthly_asof(rows(), lagged, pd.to_datetime(["2026-08-01"]), link=LINK)
    assert f["toy_sales"].tolist() == [1.0]                                      # AAA's Q2 is usable only from 2026-08-05
    old = monthly_asof(rows(), DS, pd.to_datetime(["2026-11-01"]), link=LINK, max_age_days=60, prefix="x")
    assert old.columns.tolist() == ["permno", "month", "x_sales"] and old["permno"].tolist() == [3]


def test_identifiers_need_a_link_and_a_market_series_is_refused():
    with pytest.raises(ValueError, match="pass `link`"):
        monthly_asof(rows(), DS, pd.to_datetime(["2026-11-01"]))
    market = Dataset(name="idx", licence="x", entity="market", observed="d", known="k", columns={"v": "float64"})
    with pytest.raises(ValueError, match="market series"):
        monthly_asof(pd.DataFrame(), market, pd.to_datetime(["2026-11-01"]))


def test_a_permno_dataset_needs_no_link_and_no_months_gives_an_empty_frame_with_the_columns():
    ds = Dataset(name="p", licence="x", entity="permno", observed="d", known="k", columns={"v": "float64"})
    r = pd.DataFrame({"permno": ["10"], "d": pd.to_datetime(["2026-01-31"]), "k": pd.to_datetime(["2026-02-15"]), "v": [3.0]})
    assert monthly_asof(r, ds, pd.to_datetime(["2026-03-01"]))["p_v"].tolist() == [3.0]
    assert monthly_asof(r, ds, []).columns.tolist() == ["permno", "month", "p_v"]
