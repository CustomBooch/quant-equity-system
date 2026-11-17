"""quant.data.instruments.crsp: the latest CUSIP and ticker of each permno, from the engine work database (a real DuckDB file here)."""
from __future__ import annotations

import duckdb
import pytest

from quant.data.instruments import crsp as M


@pytest.fixture()
def work_db(tmp_path, monkeypatch):
    """An engine work database whose crsp_daily table has two dates for one stock; the later identifiers must win."""
    rows = [(1, "2026-01-02", "OLDCUSIP", "OLD"), (1, "2026-06-30", "57636Q10", "MA"), (2, "2026-06-30", "99999999", "XYZ")]
    path = tmp_path / "work.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE crsp_daily (permno INTEGER, date DATE, cusip VARCHAR, ticker VARCHAR)")
    con.executemany("INSERT INTO crsp_daily VALUES (?, ?, ?, ?)", rows)
    con.close()
    monkeypatch.setattr(M, "WORK_DB", path)
    return path


def test_latest_cusip_and_ticker_of_each_permno_come_from_the_newest_date(work_db):
    ids = M.crsp_ids([1, 2])
    assert ids.loc[1, "cusip"] == "57636Q10" and ids.loc[1, "ticker"] == "MA" and ids.loc[2, "ticker"] == "XYZ"


def test_no_permnos_give_an_empty_frame_without_querying(work_db):
    assert M.crsp_ids([]).empty and list(M.crsp_ids([]).columns) == ["cusip", "ticker"]
