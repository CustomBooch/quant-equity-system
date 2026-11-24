"""The daily Geopolitical Risk source: the author's spreadsheet read to one row a day. The .xls reader is replaced (no library here writes the old format)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest
from quant.data.sources.plugins import gpr_daily as G

TODAY = dt.date(2026, 11, 18)
REQ = PullRequest(dt.date(2020, 1, 1), dt.date(2026, 11, 17))


@pytest.fixture
def sheet(monkeypatch):
    """The spreadsheet behind `get` and read_excel: state.frame is what the reader returns (three days and a blank row, the extra columns of the real file included)."""
    state = type("State", (), {})()
    state.frame = pd.DataFrame({"DAY": [20261101, 20261102, 20261103, None], "N10D": [300, 310, 320, None], "GPRD": [90.0, 110.5, 100.0, None],
                                "GPRD_ACT": [80.0, 100.0, 95.0, None], "GPRD_THREAT": [85.0, 120.0, 99.0, None]})
    monkeypatch.setattr(G, "get", lambda url: b"x")
    monkeypatch.setattr(G.pd, "read_excel", lambda *a, **k: state.frame)
    return state


def test_each_day_is_one_row_with_the_overall_index_and_its_two_parts_and_the_blank_row_is_dropped(sheet, tmp_path):
    m = ST.pull(G.GprDaily(), REQ, root=tmp_path, today=TODAY)
    d = ST.load(G.GprDaily(), root=tmp_path)
    assert m["rows"] == 3 and d["date"].dt.date.tolist()[1] == dt.date(2026, 11, 2)
    assert d.iloc[1]["gpr"] == 110.5 and d.iloc[1]["gpr_acts"] == 100.0 and d.iloc[1]["gpr_threats"] == 120.0


def test_only_days_inside_the_request_are_returned(sheet):
    out = G.GprDaily().fetch(PullRequest(dt.date(2026, 11, 2), dt.date(2026, 11, 2)))
    assert out["date"].dt.date.tolist() == [dt.date(2026, 11, 2)]


def test_a_spreadsheet_that_lost_a_column_stops_the_pull(sheet):
    sheet.frame = sheet.frame.drop(columns="GPRD_ACT")
    with pytest.raises(ValueError, match="no column.*GPRD_ACT"):
        G.GprDaily().fetch(REQ)


def test_a_download_above_the_limit_is_refused(monkeypatch):
    class Body:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"x" * n
    monkeypatch.setattr(G.urllib.request, "urlopen", lambda *a, **k: Body())
    monkeypatch.setattr(G, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="larger than the 10 byte limit"):
        G.get("https://example.org/f.xls")
