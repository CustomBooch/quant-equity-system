"""quant.data.instruments.t212: CRSP identifiers from the engine work database (a real DuckDB file here) mapped to Trading 212 lines."""
from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from quant.data.instruments import t212 as M


def instrument(ticker, isin, short, ccy, kind="STOCK"):
    return {"ticker": ticker, "isin": isin, "shortName": short, "currencyCode": ccy, "type": kind, "name": short, "maxOpenQuantity": 1000.0}


INSTRUMENTS = pd.DataFrame([
    instrument("M4Id_EQ", "US57636Q1040", "M4I", "EUR"), instrument("MA_US_EQ", "US57636Q1040", "MA", "USD"),     # one stock, two lines: the USD line wins
    instrument("XYZ_US_EQ", "US0000000000", "XYZ", "USD"),                                                       # reachable by short name only
    instrument("ABCd_EQ", "US1111111110", "ABCd", "EUR"),                                                        # only a EUR line
    instrument("QQQ_US_EQ", "US2222222220", "QQQ", "USD", kind="ETF"),                                           # not a stock: never matched
])


@pytest.fixture()
def work_db(tmp_path, monkeypatch):
    """An engine work database whose crsp_daily table has two dates per stock; the later identifiers must win."""
    rows = [(1, "2026-01-02", "OLDCUSIP", "OLD"), (1, "2026-06-30", "57636Q10", "MA"), (2, "2026-06-30", "99999999", "XYZ"), (3, "2026-06-30", "11111111", "ABC"),
            (4, "2026-06-30", "22222222", "QQQ")]
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
    assert M.crsp_ids([]).empty


def test_usd_line_wins_name_fallback_works_and_non_usd_or_non_stock_lines_are_not_tradable(work_db):
    m = M.map_permnos([1, 2, 3, 4], INSTRUMENTS)
    assert m.loc[1, "t212"] == "MA_US_EQ" and m.loc[1, "ccy"] == "USD"
    assert m.loc[2, "t212"] == "XYZ_US_EQ"                                       # CUSIP has no ISIN match; the CRSP ticker equals the short name
    assert m.loc[3, "instrument"] == "ABCd_EQ" and m.loc[3, "t212"] is None      # mapped, but to a EUR line: not tradable in the USD book
    assert m.loc[4, "instrument"] is None and m.loc[4, "t212"] is None           # the only line with that ISIN is an ETF


def test_the_newest_instrument_list_is_used(tmp_path, monkeypatch):
    for day, n in (("2026-09-01", 1), ("2026-10-18", 2)):
        d = tmp_path / f"data/raw_ext/t212_{day}"
        d.mkdir(parents=True)
        (d / "instruments.json").write_text(json.dumps(INSTRUMENTS.head(n).to_dict("records")))
    monkeypatch.setattr(M, "REPO", tmp_path)
    assert len(M.latest_instruments()) == 2


def test_a_missing_instrument_list_is_an_error_naming_the_pull_command(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "REPO", tmp_path)
    with pytest.raises(FileNotFoundError, match="quant.execution.brokers.t212.pull_instruments"):
        M.latest_instruments()
