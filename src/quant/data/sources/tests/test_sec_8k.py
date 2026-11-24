"""The SEC 8-K events source: one row per company and day with the items filed, read from the local submissions extract."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest
from quant.data.sources.plugins import sec_8k as S

REQ = PullRequest(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
TODAY = dt.date(2026, 11, 18)


@pytest.fixture
def extract(tmp_path, monkeypatch):
    """A submissions extract, set as the source's path: company 1 files two 8-Ks on 2026-05-04 (results, an officer change), an 8-K/A and a 10-K that are not 8-Ks,
    one with no report date and one whose report date is after its filing date; company 2 files a material agreement; company 3 an 8-K with no items."""
    rows = [
        (1, "0000000001-26-000001", "2026-05-04", "2026-05-01", "8-K", "2.02,9.01"),
        (1, "0000000001-26-000002", "2026-05-04", "2026-05-01", "8-K", "5.02"),
        (1, "0000000001-26-000003", "2026-05-05", "2026-05-01", "8-K/A", "2.02"),
        (1, "0000000001-26-000004", "2026-05-06", None, "10-K", ""),
        (1, "0000000001-26-000005", "2026-06-10", None, "8-K", "7.01"),
        (1, "0000000001-26-000006", "2026-07-10", "2026-07-20", "8-K", "8.01"),
        (2, "0000000002-26-000001", "2026-05-04", "2026-05-04", "8-K", "1.01"),
        (3, "0000000003-26-000001", "2026-05-04", "2026-05-04", "8-K", None),
    ]
    d = pd.DataFrame(rows, columns=["cik", "accessionNumber", "filingDate", "reportDate", "form", "items"])
    d["filingDate"] = pd.to_datetime(d["filingDate"])
    d["reportDate"] = pd.to_datetime(d["reportDate"])
    d["cik"] = d["cik"].astype("int64")
    p = tmp_path / "extract.parquet"
    d.to_parquet(p, index=False)
    monkeypatch.setattr(S.Sec8kEvents, "path", p)
    return p


def test_events_are_one_row_per_company_and_day_with_all_items_and_only_8_ks(extract, tmp_path):
    m = ST.pull(S.Sec8kEvents(), REQ, root=tmp_path / "store", today=TODAY)
    rows = ST.load(S.Sec8kEvents(), root=tmp_path / "store").set_index(["cik", "report_date", "filing_date"])
    first = rows.loc[("1", pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-04"))]
    assert m["rows"] == 5 and first["items"] == "2.02,5.02,9.01" and first["n_filings"] == 2
    assert rows.loc[("1", pd.Timestamp("2026-06-10"), pd.Timestamp("2026-06-10"))]["items"] == "7.01"       # no report date: the filing date
    assert rows.loc[("1", pd.Timestamp("2026-07-10"), pd.Timestamp("2026-07-10"))]["items"] == "8.01"       # report date after filing: the filing date
    assert rows.loc[("3", pd.Timestamp("2026-05-04"), pd.Timestamp("2026-05-04"))]["items"] == ""


def test_a_pull_naming_companies_keeps_only_those(extract, tmp_path):
    ST.pull(S.Sec8kEvents(), PullRequest(REQ.start, REQ.end, ("2",)), root=tmp_path / "store", today=TODAY)
    assert ST.load(S.Sec8kEvents(), root=tmp_path / "store")["cik"].tolist() == ["2"]


def test_events_without_the_extract_say_where_it_should_be(tmp_path, monkeypatch):
    monkeypatch.setattr(S.Sec8kEvents, "path", tmp_path / "missing.parquet")
    with pytest.raises(FileNotFoundError, match="submissions extract not found"):
        S.Sec8kEvents().fetch(REQ)
