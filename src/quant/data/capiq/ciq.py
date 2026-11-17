"""Read and validate one monthly Capital IQ export (the 16-column "good" layout) into a typed table.

Layout of Sheet1: row r0 holds the display names (first line of each cell), r0+1 the mnemonics, r0+2 the period (a word for fundamentals, a date for market data), r0+3 the as-of date
of the fundamentals, and data starts at r0+4. The checks below encode the failures met so far, so a wrong file is refused instead of silently used:
  * annual "Latest Fiscal Year" columns instead of Last Twelve Months (the very first export)
  * numeric or SNL mnemonics that return empty columns (Income Before Extraordinary Items, SG&A)
  * a pricing date and an as-of date that do not agree (fundamentals must be as of the day after the pricing date)
Units are converted to dollars: fundamentals are $000 in the export, market capitalisation $M, shares are actual shares.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from quant.data.capiq.ids import cusip8, ticker_norm
from quant.core.xlsx import Rows, read_sheets

# display name -> (mnemonic, kind). kind: text, flow (Last Twelve Months), market (dated), date (a filing date, Last Twelve Months)
REQUIRED = {
    "Entity Name": ("SP_ENTITY_NAME", "text"), "Entity ID": ("SP_ENTITY_ID", "text"), "Issue CUSIP": ("SP_CUSIP", "text"), "Ticker": ("SP_TICKER", "text"),
    "Total Revenue": ("IQ_TOTAL_REV", "flow"), "Cost Of Goods Sold": ("IQ_COGS", "flow"), "Gross Profit": ("IQ_GP", "flow"), "Total Assets": ("IQ_TOTAL_ASSETS", "flow"),
    "Total Common Equity": ("IQ_TOTAL_COMMON_EQUITY", "flow"), "Cash from Ops.": ("IQ_CASH_OPER", "flow"), "Total Debt": ("IQ_TOTAL_DEBT", "flow"),
    "Net Income": ("IQ_NET_INC_PARENT", "flow"), "Financial Filing Date": ("IQ_FINL_FILING_DATE", "date"),
    "Market Capitalization": ("SP_MARKETCAP", "market"), "Day Close Price": ("SP_PRICE_CLOSE", "market"), "Shares Outstanding": ("SP_SHARES_OUT", "market"),
}
RENAME = {"Entity Name": "name", "Entity ID": "entity_id", "Issue CUSIP": "cusip9", "Ticker": "ticker", "Total Revenue": "revenue", "Cost Of Goods Sold": "cogs", "Gross Profit": "gross_profit",
          "Total Assets": "assets", "Total Common Equity": "equity", "Cash from Ops.": "cfo", "Total Debt": "debt", "Net Income": "net_income", "Financial Filing Date": "filing_date",
          "Market Capitalization": "mcap", "Day Close Price": "close", "Shares Outstanding": "shares"}
FLOW = ["revenue", "cogs", "gross_profit", "assets", "equity", "cfo", "debt", "net_income"]
LTM = "Last Twelve Months"
NA_TOKENS = {"NA", "NM", "-", "", "N/A", "NaN"}


class CiqFormatError(Exception):
    """The export is not the expected layout, or its dates do not agree."""


@dataclass
class CiqMonth:
    """One fundamentals export: the market-data pricing date, the fundamentals as-of date (the day after), the company table in dollars, the file."""
    pricing_date: pd.Timestamp
    asof_date: pd.Timestamp
    table: pd.DataFrame
    path: str


def cell_date(text: object) -> pd.Timestamp | None:
    """A mm/dd/yyyy cell as a Timestamp; None if the text is not such a date (a blank or "nan" cell gives NaT, as pandas parses it)."""
    try:
        return pd.to_datetime(str(text).strip(), format="%m/%d/%Y")
    except ValueError:                                  # what pandas raises for text that does not parse or is out of range (OutOfBoundsDatetime)
        return None


def excel_serial(x: object) -> pd.Timestamp:
    """An Excel serial day number (1900 date system, may carry a time of day) as a Timestamp; NaT if the cell is not a number."""
    try:
        return pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(x))         # type: ignore[arg-type]  # any cell text; a non-number raises below
    except (TypeError, ValueError):
        return pd.NaT                                                             # type: ignore[return-value]  # NaT is how a datetime column holds "no date"


def _shown(d: pd.Timestamp | None) -> str:
    """A parsed date cell for an error message: the date, or "unreadable" for a cell that did not parse."""
    return "unreadable" if d is None else str(d.date())


def screening_sheet(path: str | Path) -> tuple[Rows, int]:
    """Sheet1 of a Capital IQ screening export and the number of its header row (first cell "Entity Name"; the mnemonic, period and as-of rows follow it,
    then one company per row). CiqFormatError if there is no such sheet or row."""
    rows = read_sheets(path).get("Sheet1")
    if not rows:
        raise CiqFormatError("no Sheet1")
    r0 = next((r for r in sorted(rows) if (rows[r].get("A") or "").split("\n")[0].strip() == "Entity Name"), None)
    if r0 is None:
        raise CiqFormatError("header row with 'Entity Name' not found")
    return rows, r0


def numeric(col: pd.Series) -> pd.Series:
    """Cell texts as numbers: Capital IQ's not-available tokens (NA_TOKENS) and any other text that is not a number become NaN."""
    return pd.to_numeric(col.where(~col.isin(NA_TOKENS)), errors="coerce")


def text(col: pd.Series) -> pd.Series:
    """Cell texts with Capital IQ's not-available tokens (NA_TOKENS) as missing."""
    return col.where(~col.isin(NA_TOKENS))


def load_month(path: str | Path) -> CiqMonth:
    """Read and validate one monthly fundamentals export (module docstring); CiqFormatError naming every problem found if it is not the expected layout."""
    rows, r0 = screening_sheet(path)
    head = {c: (v or "").split("\n")[0].strip() for c, v in rows[r0].items()}
    col_of = {v: c for c, v in head.items() if v}
    mnem, period, asof = rows.get(r0 + 1, {}), rows.get(r0 + 2, {}), rows.get(r0 + 3, {})
    problems = []
    for disp, (want, kind) in REQUIRED.items():
        c = col_of.get(disp)
        if c is None:
            problems.append(f"missing column '{disp}'")
            continue
        if (mnem.get(c) or "").strip() != want:
            problems.append(f"'{disp}': mnemonic {mnem.get(c)!r}, expected {want!r}")
        if kind in ("flow", "date") and (period.get(c) or "").strip() != LTM:
            problems.append(f"'{disp}': period {period.get(c)!r}, expected {LTM!r}")
        if kind == "market" and cell_date(period.get(c)) is None:
            problems.append(f"'{disp}': no pricing date in the period row ({period.get(c)!r})")
    if problems:
        raise CiqFormatError("; ".join(problems))
    pdates = {cell_date(period[col_of[d]]) for d in ("Market Capitalization", "Day Close Price", "Shares Outstanding")}
    if len(pdates) != 1:
        raise CiqFormatError(f"market data carry different pricing dates: {sorted(_shown(x) for x in pdates)}")
    pricing = pdates.pop()
    if pricing is None:                                 # cannot happen: every market column's period was checked to be a date above
        raise CiqFormatError("no pricing date in the period row")
    asofs = {cell_date(asof.get(col_of[d])) for d in ("Total Revenue", "Total Assets", "Total Common Equity", "Net Income")}
    if asofs != {pricing + pd.Timedelta(days=1)}:
        raise CiqFormatError(f"fundamentals as-of {sorted(_shown(x) for x in asofs)} is not the day after the pricing date {pricing.date()}")
    recs = []
    for r in sorted(k for k in rows if k >= r0 + 4):
        row = rows[r]
        if not (row.get(col_of["Entity Name"]) or "").strip():
            continue
        recs.append({RENAME[d]: row.get(c) for d, c in ((d, col_of[d]) for d in REQUIRED)})
    df = pd.DataFrame(recs)
    for c in FLOW + ["mcap", "close", "shares"]:
        df[c] = numeric(df[c])
    for c in ("cusip9", "ticker", "entity_id"):
        df[c] = text(df[c])
    df[FLOW] = df[FLOW] * 1e3                  # $000 -> $
    df["mcap"] = df["mcap"] * 1e6              # $M  -> $
    df["filing_date"] = df["filing_date"].map(excel_serial)
    df["cusip8"] = cusip8(df["cusip9"])
    tag = df["name"].str.extract(r"\(([A-Za-z]+):([^)]+)\)\s*$")
    df["exch"] = tag[0]
    df["ticker_norm"] = ticker_norm(df["ticker"])
    df["late_filing"] = df["filing_date"].notna() & (df["filing_date"] > pricing)
    return CiqMonth(pricing, pricing + pd.Timedelta(days=1), df.reset_index(drop=True), str(path))
