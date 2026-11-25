"""Capital IQ monthly bridge exports (CapIQ_<Mon>_bridge.xlsx) and the consensus and surprise features built from them (SPEC_OCT_FILL.md H and I).

Layout: a screening export like the other Capital IQ files (header row "Entity Name", mnemonic row, period row, as-of date row, one company per row). Columns are keyed
"<mnemonic>|<period>" (SP_EPS_EST|FY+1, SP_EST_ACT_EPS|FQ0, ...). Every column carries one common as-of date, the first of a month, which is checked.
Companies are matched to permnos by CUSIP8 only (these files carry no ticker), the largest market equity winning a shared CUSIP8.

Definitions (panel/pit/ibes.py, Capital IQ in place of I/B/E/S):
  fy2_growth = (FY+2 mean - FY+1 mean) / |FY+1 mean|                 rev_sal_fy1_1m = (revenue FY+1 mean - the same one file earlier) / |earlier|
  consensus = actual - difference (Capital IQ's announcement-time consensus)
  surprise_sym = (actual - consensus) / (|actual| + |consensus|)      sales_surprise_sym: the same with revenue
  surprise_disp = difference / FQ+1 standard deviation in the latest earlier file dated before the announcement (no other report date in between)
  pos_surprise_4q = number of FQ0..FQ-3 differences above zero, all four present
The announcement date of FQ0 is the company's latest Compustat report date before the decision month; knowledge date = that date + 1, at most 120 days old.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from quant.data.capiq.ciq import CiqFormatError, cell_date, numeric, screening_sheet
from quant.data.capiq.ids import cusip8

MONTHS = {6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
SURP_WINDOW_DAYS = 120
REQUIRED = ["SP_EPS_EST|FY+1", "SP_EPS_EST|FY+2", "SP_REV_EST|FY+1", "SP_EPS_STDDEV_EST|FQ+1", "SP_EST_ACT_REV|FQ0", "SP_EST_REV_DIFF|FQ0"] + \
    [f"{m}|FQ{q}" for m in ("SP_EST_ACT_EPS", "SP_EST_EPS_DIFF") for q in ("0", "-1", "-2", "-3")]
FY1 = "SP_EPS_EST|FY+1"
SURP_COLS = ["surprise_sym", "surprise_disp", "pos_surprise_4q", "sales_surprise_sym"]
CONS_COLS = ["fy2_growth", "rev_sal_fy1_1m"]


@dataclass
class BridgeFile:
    """One monthly bridge export: its as-of date (the first of a month), the company table, the file."""
    asof: pd.Timestamp
    table: pd.DataFrame                  # one row per company: cusip8 plus the REQUIRED columns (float)
    path: str


def path_for(ciq_dir: Path, month: pd.Timestamp) -> Path:
    """The bridge export of decision `month` in `ciq_dir` (CapIQ_<Mon>_bridge.xlsx)."""
    return Path(ciq_dir) / f"CapIQ_{MONTHS[month.month]}_bridge.xlsx"


def load_bridge(path: str | Path, fy1_by_cusip8: pd.Series | None = None) -> BridgeFile:
    """Read and validate one bridge export (module docstring); CiqFormatError if SP_CUSIP or a REQUIRED column is missing or the columns do not share
    one as-of date on the first of a month. From November 2026 the export leaves out the FY+1 EPS mean, which the estimates file of the same date
    carries: pass it as `fy1_by_cusip8` (FY+1 mean indexed by CUSIP8) and it fills that column (the wiki page Home 5b)."""
    rows, r0 = screening_sheet(path)
    mnem, period, asof = rows.get(r0 + 1, {}), rows.get(r0 + 2, {}), rows.get(r0 + 3, {})
    key = {c: f"{(v or '').strip()}|{(period.get(c) or '').strip()}" for c, v in mnem.items() if v}
    col_of = {k: c for c, k in key.items()}
    cus = next((c for c, v in mnem.items() if (v or "").strip() == "SP_CUSIP"), None)
    from_est = fy1_by_cusip8 is not None and FY1 not in col_of
    need = [k for k in REQUIRED if not (from_est and k == FY1)]
    missing = [k for k in need if k not in col_of]
    if cus is None or missing:
        raise CiqFormatError(f"missing columns: {(['SP_CUSIP'] if cus is None else []) + missing}")
    dates = {cell_date(asof.get(col_of[k])) for k in need}
    if len(dates) != 1 or None in dates:
        raise CiqFormatError(f"the columns do not share one as-of date: {sorted(str(d) for d in dates)}")
    a = cast(pd.Timestamp, dates.pop())                                            # not None: checked just above
    if a.day != 1:
        raise CiqFormatError(f"as-of date {a.date()} is not the first of a month")
    recs = []
    for r in sorted(k for k in rows if k >= r0 + 4):
        cells = rows[r]
        if (cells.get(cus) or "").strip():
            recs.append({"cusip9": cells.get(cus), **{k: cells.get(col_of[k]) for k in need}})
    df = pd.DataFrame(recs)
    for k in need:
        df[k] = numeric(df[k])
    df["cusip8"] = cusip8(df["cusip9"])
    if from_est and fy1_by_cusip8 is not None:
        df[FY1] = df["cusip8"].map(fy1_by_cusip8[~fy1_by_cusip8.index.duplicated()]).to_numpy(dtype="float64", na_value=np.nan)   # plain floats, NaN when unmatched (pandas 3 maps to NA)
    return BridgeFile(a, df, str(path))


def by_permno(f: BridgeFile, ids: pd.DataFrame) -> pd.DataFrame:
    """`ids`: permno, cusip, me. One row per permno (CUSIP8 match; a CUSIP8 shared by two permnos goes to the one with the larger market equity)."""
    s = ids.assign(c8=cusip8(ids["cusip"])).sort_values("me", ascending=False).dropna(subset=["c8"]).drop_duplicates("c8")
    t = f.table.merge(s[["c8", "permno"]], left_on="cusip8", right_on="c8").drop_duplicates("permno")
    return t.assign(permno=t["permno"].astype("int64")).set_index("permno")


def consensus_features(cur: pd.DataFrame, prev: pd.DataFrame | None, permnos: pd.Index) -> pd.DataFrame:
    """fy2_growth from the current file; rev_sal_fy1_1m against the file one month earlier (`prev`, by permno)."""
    c = cur.reindex(permnos)
    fy1, fy2 = c["SP_EPS_EST|FY+1"], c["SP_EPS_EST|FY+2"]
    out = pd.DataFrame(index=permnos)
    out["fy2_growth"] = (fy2 - fy1) / fy1.abs().where(fy1 != 0)
    if prev is None:
        out["rev_sal_fy1_1m"] = np.nan
    else:
        r0, r1 = c["SP_REV_EST|FY+1"], prev.reindex(permnos)["SP_REV_EST|FY+1"]
        out["rev_sal_fy1_1m"] = (r0 - r1) / r1.abs().where(r1 != 0)
    return out


def sym(actual: pd.Series, cons: pd.Series) -> pd.Series:
    d = actual.abs() + cons.abs()
    return (actual - cons) / d.where(d > 0)


def surprise_features(files: dict[pd.Timestamp, pd.DataFrame], rdq: pd.DataFrame, month: pd.Timestamp, permnos: pd.Index) -> pd.DataFrame:
    """Surprise features for decision `month`. `files`: as-of date -> table by permno (the file of `month` and the earlier ones). `rdq`: permno, rdq (Compustat report dates)."""
    cur = files[month].reindex(permnos)
    r = rdq[rdq["rdq"] < month].sort_values("rdq")
    last = r.groupby("permno")["rdq"].last().reindex(permnos)
    kd = last + pd.Timedelta(days=1)
    ok = kd.notna() & (kd < month) & ((month - kd).dt.days <= SURP_WINDOW_DAYS)
    out = pd.DataFrame(index=permnos)
    act, diff = cur["SP_EST_ACT_EPS|FQ0"], cur["SP_EST_EPS_DIFF|FQ0"]
    out["surprise_sym"] = sym(act, act - diff).where(ok)
    ract, rdiff = cur["SP_EST_ACT_REV|FQ0"], cur["SP_EST_REV_DIFF|FQ0"]
    out["sales_surprise_sym"] = sym(ract, ract - rdiff).where(ok)
    diffs = cur[[f"SP_EST_EPS_DIFF|FQ{q}" for q in ("0", "-1", "-2", "-3")]]
    out["pos_surprise_4q"] = (diffs > 0).sum(axis=1).astype(float).where(diffs.notna().all(axis=1) & ok)
    # dispersion: the FQ+1 standard deviation of the latest earlier file dated before the announcement, with no other report date between that file and the announcement
    std = pd.Series(np.nan, index=permnos)
    for d in sorted((d for d in files if d < month), reverse=True):
        take = std.isna() & last.notna() & (d < last) & ((last - d).dt.days <= SURP_WINDOW_DAYS)
        if not take.any():
            continue
        between = r[(r["rdq"] >= d)].groupby("permno")["rdq"].nunique().reindex(permnos).fillna(0)
        take &= between == 1                                                   # only the announcement itself falls on or after the file date
        std[take] = files[d].reindex(permnos)["SP_EPS_STDDEV_EST|FQ+1"][take]
    out["surprise_disp"] = (diff / std.where(std > 0)).where(ok)
    out["kd_surp_ciq"] = kd.where(ok)
    return out
