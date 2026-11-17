"""DuckDB helpers shared by the release builders (assemble, full_fill) and the SEC pull: date literals and queries that return exactly one row."""
from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd


def day(m: pd.Timestamp) -> str:
    """`m` as a DuckDB date literal, DATE 'YYYY-MM-DD'."""
    return f"DATE '{m.date()}'"


def quote(value: str) -> str:
    """`value` as a DuckDB string literal: single quotes doubled, so a path or a name read from outside cannot end the literal and add statements."""
    return "'" + value.replace("'", "''") + "'"


def one_row(con: duckdb.DuckDBPyConnection, sql: str, error: type[Exception] = RuntimeError) -> tuple[Any, ...]:
    """The row of a query that always returns exactly one (an aggregate, a one-row coverage table); `error` (RuntimeError by default) naming the query otherwise."""
    row = con.execute(sql).fetchone()
    if row is None:
        raise error(f"no row returned by {sql}")
    return row
