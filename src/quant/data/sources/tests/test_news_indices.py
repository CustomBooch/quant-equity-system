"""The news-based indices source: eleven FRED series (a missing value is a '.') joined by month, a month known at its end, the first months of a short series left empty."""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse

import pytest

from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest
from quant.data.sources.plugins import news_indices as N

TODAY = dt.date(2026, 11, 18)
REQ = PullRequest(dt.date(2020, 1, 1), dt.date(2026, 11, 17))


@pytest.fixture
def fred(monkeypatch):
    """The FRED API behind `get`: state.rows maps a series id to its observations as [date, value] pairs (default: 2026-08 valued 1.5 and 2026-09 valued 2.5); state.asked lists the ids requested."""
    state = type("State", (), {"rows": {}, "asked": []})()

    def get(url):
        fred_id = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["series_id"]
        state.asked.append(fred_id)
        return json.dumps({"observations": [{"date": d, "value": v} for d, v in state.rows.get(fred_id, [["2026-08-01", "1.5"], ["2026-09-01", "2.5"]])]}).encode()

    monkeypatch.setattr(N, "get", get)
    monkeypatch.setenv("FRED_API_KEY", "k")
    return state


def test_each_series_is_one_column_and_a_month_is_known_at_its_end(fred, tmp_path):
    ST.pull(N.NewsIndices(), REQ, root=tmp_path, today=TODAY)
    d = ST.load(N.NewsIndices(), root=tmp_path)
    assert sorted(fred.asked) == sorted(N.SERIES.values()) and len(d) == 2
    assert d.iloc[0]["emv_overall"] == 1.5 and str(d.iloc[0]["published"].date()) == "2026-08-31"


def test_a_series_that_starts_later_or_ends_earlier_leaves_the_other_months_empty(fred, tmp_path):
    fred.rows["EPUTRADE"] = [["2026-08-01", "9.0"]]
    fred.rows["EMVCOMMMKT"] = [["2026-08-01", "."], ["2026-09-01", "4.0"]]
    d = N.NewsIndices().fetch(REQ).set_index("month")
    assert d["epu_trade"].tolist()[0] == 9.0 and d["epu_trade"].isna().tolist() == [False, True] and d["emv_commodities"].isna().tolist() == [True, False]


def test_only_months_known_inside_the_request_are_returned(fred):
    assert [m.date() for m in N.NewsIndices().fetch(PullRequest(dt.date(2026, 9, 1), dt.date(2026, 11, 1)))["month"]] == [dt.date(2026, 9, 1)]


def test_no_key_says_where_to_get_one(fred, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY")
    with pytest.raises(RuntimeError, match="FRED_API_KEY is not set"):
        N.NewsIndices().fetch(REQ)
