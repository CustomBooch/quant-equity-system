"""Excel workbook for pulling Capital IQ analyst estimates for the investable universe, with the formulas filled in.

    python -m quant.data.capiq.request_workbook --panel data/master/master_2026-10-23/master_panel.parquet --state panel/_build/ext_2026-10-22a/build/tables/security_state.parquet \
        --out data/master/ciq_estimates_request_2026-10-23.xlsx

Universe: stocks that are in_investable in any month from 2026-06 on, with identifiers from the end-of-June CRSP state (ticker, CUSIP8 and the computed 9th check digit), biggest first.
Sheets: README, Config (identifier choice, snapshot dates, field names/mnemonics/periods), Universe, Pilot (30 stocks, first snapshot) and one sheet per snapshot date.
Every snapshot cell is a formula built from a template, so the formula syntax and the mnemonics are single settings: the mnemonics live on the Config sheet, the syntax in --formula.
Nothing here has been run against the Capital IQ plugin: the mnemonics and the default formula are not confirmed. The Pilot sheet is for confirming them on 30 stocks first.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd

from quant.data.capiq.ids import cusip8
from quant.core.xlsx_write import Bold, Formula, Row, Sheet, col_letter, write_workbook

SNAPSHOTS = [("2026-06-18", "IBES has this snapshot: parity check"), ("2026-07-16", "IBES has this snapshot: parity check"), ("2026-08-20", "IBES has this snapshot: parity check, its CUSIPs cannot be mapped by the engine"),
             ("2026-09-17", "no IBES snapshot in hand: new"), ("2026-10-15", "no IBES snapshot in hand: new")]
# display name, mnemonic (NOT CONFIRMED: replace on the Config sheet with the name the plugin's function wizard shows), period
FIELDS = [
    ("EPS Normalized Est, FY+1", "IQ_EST_EPS_NORM", "FY+1"), ("EPS Normalized Est Med, FY+1", "IQ_EST_EPS_NORM_MEDIAN", "FY+1"),
    ("EPS Normalized Est - Std Dev, FY+1", "IQ_EST_EPS_NORM_STDDEV", "FY+1"), ("EPS Normalized Est - # of Est, FY+1", "IQ_EST_EPS_NORM_NUM_EST", "FY+1"),
    ("EPS Normalized Est Date, FY+1", "IQ_EST_EPS_NORM_DATE", "FY+1"), ("EPS Normalized Est Fiscal Year, FY+1", "IQ_EST_EPS_NORM_FISCAL_YEAR", "FY+1"),
    ("EPS Normalized Est, FY+2", "IQ_EST_EPS_NORM", "FY+2"), ("EPS Normalized Est - # of Est, FY+2", "IQ_EST_EPS_NORM_NUM_EST", "FY+2"),
    ("EPS Normalized Est Fiscal Year, FY+2", "IQ_EST_EPS_NORM_FISCAL_YEAR", "FY+2"),
    ("EPS (Excl. Excep, GW) Est, FY+1", "IQ_EST_EPS_EXCL", "FY+1"), ("EPS (Excl. Excep, GW) Est - # of Est, FY+1", "IQ_EST_EPS_EXCL_NUM_EST", "FY+1"),
]
DEFAULT_FORMULA = "=CIQ({id},{mnem},{period},{asof})"
PILOT_N = 30
ID_COLS = 3                                  # Identifier, Ticker, CUSIP9 before the field columns


def cusip9(c8: str | None) -> str | None:
    """Append the standard CUSIP check digit to an 8-character CUSIP."""
    if not c8 or len(c8) != 8:
        return None
    total = 0
    for i, ch in enumerate(c8.upper()):
        v = int(ch) if ch.isdigit() else (ord(ch) - 55 if ch.isalpha() else {"*": 36, "@": 37, "#": 38}.get(ch))
        if v is None:
            return None
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return c8.upper() + str((10 - total % 10) % 10)


def universe(panel: Path, state: Path) -> pd.DataFrame:
    """Stocks investable in any month from 2026-06, biggest first, with the end-of-June CRSP identifiers (ticker, CUSIP8 and CUSIP9)."""
    con = duckdb.connect()
    u = con.execute(f"""select s.permno, s.ticker, s.cusip, s.exch, s.me from read_parquet('{state.as_posix()}') s
                        where s.smonth = DATE '2026-06-01' and s.permno in (select permno from read_parquet('{panel.as_posix()}') where in_investable and month >= DATE '2026-06-01')
                        order by s.me desc, s.permno""").df()
    u["cusip8"] = cusip8(u["cusip"])
    u["cusip9"] = u["cusip8"].map(lambda c: cusip9(c) if isinstance(c, str) else None)
    return u.drop(columns=["cusip"]).reset_index(drop=True)


def _snapshot_rows(u: pd.DataFrame, date: dt.date, template: str, first_field_cfg_row: int, n: int, title: str) -> list[Row]:
    """One snapshot sheet: the header rows (fields, mnemonics and periods from the Config sheet, the as-of date), then one formula row per stock."""
    nf = len(FIELDS)
    fc = [col_letter(ID_COLS + j) for j in range(nf)]
    rows: list[Row] = [[Bold(title), "Check the first cells, then Copy, Paste Special as Values, and save as capiq_est_<date>.xlsx"]]
    rows.append([Bold("Identifier"), Bold("Ticker"), Bold("CUSIP9")] + [Bold(f[0]) for f in FIELDS])
    rows.append(["mnemonic", None, None] + [Formula(f"Config!B{first_field_cfg_row + j}") for j in range(nf)])
    rows.append(["period", None, None] + [Formula(f"Config!C{first_field_cfg_row + j}") for j in range(nf)])
    rows.append(["as of date", None, None] + [date] * nf)
    for i in range(min(n, len(u))):
        r = 6 + i
        rows.append([Formula(f"Universe!A{2 + i}"), Formula(f"Universe!B{2 + i}"), Formula(f"Universe!D{2 + i}")]
                    + [Formula(template.lstrip("=").format(id=f"$A{r}", mnem=f"{fc[j]}$3", period=f"{fc[j]}$4", asof=f"{fc[j]}$5")) for j in range(nf)])
    return rows


def build(panel: Path, state: Path, out: Path, template: str = DEFAULT_FORMULA) -> dict[str, Any]:
    """Write the request workbook to `out`; returns its size (stocks, formulas per snapshot, sheet names) and the identifiers missing."""
    u = universe(panel, state)
    n = len(u)
    cfg: list[Row] = [[Bold("Config: edit here, every sheet follows")], [],
                       ["Identifier used in the formulas (TICKER or CUSIP9)", "TICKER"], [],
                       [Bold("Snapshots"), Bold("As of date"), Bold("Note")]]
    cfg += [[f"S_{d}", dt.date.fromisoformat(d), note] for d, note in SNAPSHOTS]
    cfg += [[], [Bold("Fields"), Bold("Mnemonic (NOT CONFIRMED)"), Bold("Period"), Bold("Mnemonic confirmed?")]]
    first = len(cfg) + 1                     # Excel row of the first field
    cfg += [[d, m, p, "NO"] for d, m, p in FIELDS]
    readme: list[Row] = [[Bold("Capital IQ estimates request")], [],
              [f"Universe: {n} stocks that are investable in any month from 2026-06, biggest first (Universe sheet). Identifiers come from the end-of-June CRSP state."],
              ["Licensed-data derivative: keep it on this machine, do not share it."], [],
              [Bold("Steps")],
              ["1. Open the Pilot sheet (30 largest stocks, snapshot 2026-06-18). Check what the first cells return."],
              ["2. If they show #NAME or errors: build ONE cell with the plugin's Formula Builder (Period Type Fiscal Year, Period FY+1, As Of Date) and read its text."],
              ["   Fix the mnemonics on the Config sheet (column B). If the syntax differs, send me that formula text and I regenerate the workbook with --formula."],
              ["3. When the Pilot is right, mark the Config rows confirmed, open each S_<date> sheet, let it calculate (it is ~27,000 formulas per sheet: do one sheet at a time)."],
              ["4. Copy the sheet, Paste Special Values into a new workbook, check the as-of date row, save as capiq_est_<date>.xlsx in data/raw/Capital IQ."],
              ["Dates: use the month/day/year format, as in the fundamentals files. The as of date row of every sheet must show the snapshot date in its sheet name."],
              ["Keep the Fiscal Year columns: they show which year each estimate is for."],
              [], [Bold("Not available in the plugin list"), "up/down revision counts, EPS surprise inputs (actual EPS, quarterly estimates): see the chat notes"]]
    uni: list[Row] = [[Bold("Identifier"), Bold("Ticker"), Bold("CUSIP8"), Bold("CUSIP9"), Bold("Exchange (N/A/Q)"), Bold("PERMNO")]]
    for i, r in u.iterrows():
        row = cast(int, i) + 2                                                      # universe() returns a 0-based RangeIndex: Excel row of the stock
        uni.append([Formula('IF(Config!$B$3="CUSIP9",D%d,B%d)' % (row, row)), r["ticker"] if isinstance(r["ticker"], str) else None, r["cusip8"] if isinstance(r["cusip8"], str) else None,
                    r["cusip9"] if isinstance(r["cusip9"], str) else None, r["exch"], int(r["permno"])])
    sheets: list[Sheet] = [("README", readme, [120], None), ("Config", cfg, [52, 30, 14, 22], None), ("Universe", uni, [14, 10, 12, 12, 16, 10], (1, 1))]
    w = [14, 9, 12] + [22] * len(FIELDS)
    d0 = dt.date.fromisoformat(SNAPSHOTS[0][0])
    sheets.append((f"Pilot_{d0}", _snapshot_rows(u, d0, template, first, PILOT_N, f"Pilot: {PILOT_N} largest stocks, as of {d0}"), w, (5, 3)))
    for d, _ in SNAPSHOTS:
        dd = dt.date.fromisoformat(d)
        sheets.append((f"S_{d}", _snapshot_rows(u, dd, template, first, n, f"Capital IQ estimates as of {d}"), w, (5, 3)))
    out.parent.mkdir(parents=True, exist_ok=True)
    write_workbook(out, sheets)
    return {"stocks": n, "formulas_per_snapshot": n * len(FIELDS), "sheets": [s[0] for s in sheets], "missing_ticker": int(u["ticker"].isna().sum()), "missing_cusip": int(u["cusip8"].isna().sum())}


def main(argv: list[str] | None = None) -> int:
    """Build the workbook from the command line and print its summary."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--formula", default=DEFAULT_FORMULA, help="template with {id} {mnem} {period} {asof}")
    a = ap.parse_args(argv)
    print(build(Path(a.panel), Path(a.state), Path(a.out), a.formula))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
