"""quant.core.sql: date literals and the one-row query, on a real DuckDB connection."""
import duckdb
import pandas as pd
import pytest

from quant.core import sql
from quant.core.release import compare, pointer
from quant.core.release.release import ReleaseError


def test_a_month_is_written_as_a_duckdb_date_literal():
    assert sql.day(pd.Timestamp("2026-11-01")) == "DATE '2026-11-01'"
    assert duckdb.connect().execute(f"select {sql.day(pd.Timestamp('2026-11-01'))}").fetchone() == (pd.Timestamp("2026-11-01").date(),)


def test_one_row_returns_the_row_of_an_aggregate():
    assert sql.one_row(duckdb.connect(), "select count(*), 7") == (1, 7)


def test_a_query_with_no_row_raises_runtime_error_naming_the_query_unless_another_error_is_asked_for():
    con = duckdb.connect()
    with pytest.raises(RuntimeError, match="no row returned by select 1 where false"):
        sql.one_row(con, "select 1 where false")
    with pytest.raises(ReleaseError, match="no row returned"):
        sql.one_row(con, "select 1 where false", ReleaseError)


def test_the_release_modules_share_this_function_and_the_pointer_keeps_its_own_error():
    assert compare.one_row is sql.one_row
    with pytest.raises(ReleaseError):
        pointer._one_row(duckdb.connect(), "select 1 where false")
