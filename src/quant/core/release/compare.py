"""Which columns differ between two panel files, keyed by (permno, month). python -m quant.core.release.compare OLD.parquet NEW.parquet [--where "NOT bridge"]

Per shared column, the order-free fingerprint sum(hash(permno, month, column)) is compared, so row order does not matter and nothing is loaded into memory. Reports columns only in
one file, and the shared columns whose values differ. Read-only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import duckdb

from quant.core.sql import ident, one_row, quote

BATCH = 40                      # columns fingerprinted per scan


def columns(con: duckdb.DuckDBPyConnection, pq: str) -> list[str]:
    """Column names of a parquet file, in file order."""
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({quote(pq)})").fetchall()]


def fingerprints(con: duckdb.DuckDBPyConnection, pq: str, cols: list[str], where: str) -> dict[str, int]:
    """{column: sum(hash(permno, month, column))} over the rows matching `where`: equal for two files exactly when their (key, value) multisets agree,
    up to hash collisions; independent of row order."""
    out: dict[str, int] = {}
    for i in range(0, len(cols), BATCH):
        part = cols[i:i + BATCH]
        row = one_row(con, f"SELECT {', '.join(f'sum(hash(permno, month, {ident(c)}))' for c in part)} FROM read_parquet({quote(pq)}) WHERE {where}")
        out.update(zip(part, row, strict=True))                                   # one aggregate per column of the batch
    return out


def compare(old: Path, new: Path, where: str = "TRUE", memory: str = "8GB") -> dict[str, Any]:
    """Columns only in one file and shared columns whose values differ (keyed by permno and month), with the row counts, under the filter `where`."""
    con = duckdb.connect()
    con.execute(f"PRAGMA memory_limit='{memory}'")
    a, b = old.as_posix(), new.as_posix()
    ca, cb = columns(con, a), columns(con, b)
    shared = [c for c in ca if c in cb]
    fa, fb = fingerprints(con, a, shared, where), fingerprints(con, b, shared, where)
    rows = [one_row(con, f"SELECT count(*) FROM read_parquet({quote(p)}) WHERE {where}")[0] for p in (a, b)]
    return {"old": str(old), "new": str(new), "where": where, "rows": {"old": rows[0], "new": rows[1]}, "only_old": [c for c in ca if c not in cb], "only_new": [c for c in cb if c not in ca],
            "shared": len(shared), "changed": [c for c in shared if fa[c] != fb[c]]}


def main(argv: list[str] | None = None) -> None:
    """Print the comparison of two panel files as JSON."""
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--where", default="TRUE", help="SQL filter applied to both files, e.g. \"NOT bridge\"")
    a = ap.parse_args(argv)
    print(json.dumps(compare(Path(a.old), Path(a.new), a.where), indent=1))


if __name__ == "__main__":
    main()
