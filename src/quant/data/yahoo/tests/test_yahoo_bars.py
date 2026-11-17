"""quant.data.yahoo.yahoo_bars: the refresh keeps each stock's symbol, writes only a new folder, resumes, and records what Yahoo did not return.
Boundaries replaced: Yahoo's HTTP (urlopen) and the pause between requests; the files are real, in tmp_path."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import time
import types
import urllib.error
import urllib.parse
import urllib.request

import pytest

from quant.data.yahoo import yahoo_bars as YB
from quant.features.ciqfill import yahoo_price as yp
from quant.features.ciqfill.tests.factories import chart, write_chart

END = dt.date(2026, 10, 30)


@pytest.fixture()
def src(tmp_path):
    """The bars the current release used: three stocks, one of which Yahoo no longer returns."""
    d = tmp_path / "yahoo"
    d.mkdir()
    for permno, sym in ((1, "AAA"), (2, "BBB"), (3, "GONE")):
        write_chart(d / f"{permno}.json.gz", ["2026-09-30"], [10.0], symbol=sym)
    return d


@pytest.fixture()
def yahoo(monkeypatch):
    """The chart API behind urlopen: new bars to 2026-10-30 for AAA and BBB, no result for GONE; `fail` makes the next requests raise."""
    state = types.SimpleNamespace(urls=[], fail=0, page=0)

    def urlopen(req, timeout):
        state.urls.append(req.full_url)
        if state.fail:
            state.fail -= 1
            raise urllib.error.URLError("connection reset")
        if state.page:
            state.page -= 1
            return io.BytesIO(b"<html>Too Many Requests</html>")
        sym = urllib.parse.urlparse(req.full_url).path.rsplit("/", 1)[-1]
        if sym == "GONE":
            return io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found"}}}')
        return io.BytesIO(json.dumps(chart(["2026-10-29", "2026-10-30"], [11.0, 12.0], symbol=sym)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return state


def test_each_stock_is_refreshed_under_its_recorded_symbol_into_a_new_folder(tmp_path, src, yahoo):
    before = {f.name: f.read_bytes() for f in src.iterdir()}
    man = YB.refresh(src, tmp_path / "new", END)
    assert [urllib.parse.urlparse(u).path.rsplit("/", 1)[-1] for u in yahoo.urls] == ["AAA", "BBB", "GONE"]
    assert {f.name: f.read_bytes() for f in src.iterdir()} == before                         # the release's inputs are untouched
    body = gzip.decompress((tmp_path / "new/1.json.gz").read_bytes())
    assert man["files"]["1.json.gz"]["sha256"] == hashlib.sha256(body).hexdigest() and json.loads((tmp_path / "new/MANIFEST.json").read_text()) == man


def test_the_request_covers_the_whole_history_to_the_end_day(tmp_path, src, yahoo):
    YB.refresh(src, tmp_path / "new", END)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(yahoo.urls[0]).query)
    assert (int(q["period1"][0]), int(q["period2"][0])) == (YB.epoch(YB.START), YB.epoch(END + dt.timedelta(days=1)))   # period2 is exclusive: the end day is included


def test_the_refreshed_bars_are_what_the_bridge_reader_reads(tmp_path, src, yahoo):
    YB.refresh(src, tmp_path / "new", END)
    assert yp.last_close(2, tmp_path / "new") == (12.0, dt.date(2026, 10, 30))
    assert sorted(yp.load_yahoo([1, 2, 3], tmp_path / "new")["id"].unique()) == [1, 2]


def test_a_symbol_yahoo_no_longer_returns_is_recorded_not_guessed(tmp_path, src, yahoo):
    man = YB.refresh(src, tmp_path / "new", END)
    assert man["not_returned"] == {"3": "GONE"} and not (tmp_path / "new/3.json.gz").exists()


def test_a_second_run_resumes_without_asking_again(tmp_path, src, yahoo):
    YB.refresh(src, tmp_path / "new", END)
    n = len(yahoo.urls)
    YB.refresh(src, tmp_path / "new", END)
    assert len(yahoo.urls) == n


def test_the_source_folder_or_a_folder_of_another_end_day_is_refused(tmp_path, src, yahoo):
    with pytest.raises(SystemExit, match="new folder"):
        YB.refresh(src, src, END)
    YB.refresh(src, tmp_path / "new", END)
    with pytest.raises(SystemExit, match="holds bars to 2026-10-30"):
        YB.refresh(src, tmp_path / "new", END + dt.timedelta(days=1))


def test_a_network_error_is_retried_and_raised_when_it_persists(tmp_path, src, yahoo):
    yahoo.fail = 1
    YB.refresh(src, tmp_path / "a", END)
    assert (tmp_path / "a/1.json.gz").exists() and len(yahoo.urls) == 4                    # one failed request, then AAA, BBB, GONE
    yahoo.fail = YB.TRIES
    with pytest.raises(urllib.error.URLError):
        YB.refresh(src, tmp_path / "b", END)


def test_a_page_that_is_not_a_chart_is_retried_not_taken_for_a_missing_symbol(tmp_path, src, yahoo):
    yahoo.page = 1                                                                           # a rate-limit page for the first request
    man = YB.refresh(src, tmp_path / "new", END)
    assert (tmp_path / "new/1.json.gz").exists() and man["not_returned"] == {"3": "GONE"}
