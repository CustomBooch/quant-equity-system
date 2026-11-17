"""13F ownership vintages from the SEC Form 13F data sets, with the engine's definitions (panel/pit/ownership.py) and the substitutions stated in SPEC_OCT_FILL.md C.

Thomson Reuters' 13F table, which the engine reads, is a processed product; the SEC data sets are the raw filings. The substitutions:
  manager = filer CIK; per manager and quarter the latest original 13F-HR, or the latest amendment of type RESTATEMENT in its place, plus every amendment of type NEW HOLDINGS;
  shares = SSHPRNAMT where SSHPRNAMTTYPE = SH and PUTCALL is empty, summed by manager and CUSIP8; a manager with a report for the previous quarter and none for this
  quarter is carried forward one quarter (rdate < fdate: the stale part, change 0); change = shares minus the manager's previous-quarter shares (shares if none);
  closed positions have no row; fdate = period of report; knowledge date = fdate + 60 days.
Only calendar quarter-ends reported by at least MIN_MANAGERS managers are quarters (late amendments for old quarters would otherwise make small partial periods), and
the carry-forward and change use only the adjacent earlier quarter.
Each data-set zip holds SUBMISSION.tsv, COVERPAGE.tsv and INFOTABLE.tsv; the zips cover filing windows, so a quarter's reports (and late amendments) can sit in two zips.
"""
from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import duckdb
import numpy as np
import pandas as pd

IO_COLS = ["io_frac", "io_n_mgr", "io_hhi", "io_stale_frac", "io_frac_chg", "io_breadth_chg", "io_buy_sell", "io_net_change"]
KNOWLEDGE_LAG, WINDOW_DAYS = 60, 120
MIN_MANAGERS = 1000


def extract(zip_path: Path) -> Path:
    """Unpack the three tables once, next to the zip."""
    d = zip_path.with_suffix("")
    if not (d / "INFOTABLE.tsv").exists():
        with zipfile.ZipFile(zip_path) as zf:
            for n in zf.namelist():
                base = n.split("/")[-1]
                if base in ("SUBMISSION.tsv", "COVERPAGE.tsv", "INFOTABLE.tsv"):
                    d.mkdir(exist_ok=True)
                    (d / base).write_bytes(zf.read(n))
    return d


def reports(dirs: list[Path], con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Holdings reports: accession, cik, period, filing date, amendment type ('' for an original)."""
    parts = []
    for d in dirs:
        parts.append(con.execute(f"""
            SELECT s.ACCESSION_NUMBER AS accession, CAST(s.CIK AS BIGINT) AS cik, strptime(s.PERIODOFREPORT, '%d-%b-%Y')::DATE AS period,
                   strptime(s.FILING_DATE, '%d-%b-%Y')::DATE AS filed, s.SUBMISSIONTYPE AS stype,
                   coalesce(upper(c.AMENDMENTTYPE), '') AS atype, coalesce(c.REPORTTYPE, '') AS rtype
            FROM read_csv('{(d / "SUBMISSION.tsv").as_posix()}', delim='\t', header=true, all_varchar=true, quote='') s
            LEFT JOIN read_csv('{(d / "COVERPAGE.tsv").as_posix()}', delim='\t', header=true, all_varchar=true, quote='') c USING (ACCESSION_NUMBER)
            WHERE s.SUBMISSIONTYPE IN ('13F-HR', '13F-HR/A')""").df())
    r = pd.concat(parts, ignore_index=True).drop_duplicates("accession")
    return r[~r["rtype"].str.upper().str.contains("NOTICE")]


def select(r: pd.DataFrame) -> pd.DataFrame:
    """The reports that make up each manager's holdings for each quarter: the base (latest RESTATEMENT amendment, else latest original) and every NEW HOLDINGS
    amendment filed on or after the base."""
    r = r.sort_values(["cik", "period", "filed", "accession"])
    orig = r[r["stype"] == "13F-HR"].groupby(["cik", "period"]).tail(1)
    rest = r[(r["stype"] == "13F-HR/A") & (r["atype"] == "RESTATEMENT")].groupby(["cik", "period"]).tail(1)
    base = pd.concat([rest, orig]).drop_duplicates(["cik", "period"], keep="first")
    new = r[(r["stype"] == "13F-HR/A") & (r["atype"] == "NEW HOLDINGS")].merge(base[["cik", "period", "filed"]].rename(columns={"filed": "base_filed"}), on=["cik", "period"])
    new = new[new["filed"] >= new["base_filed"]].drop(columns="base_filed")
    return pd.concat([base, new], ignore_index=True)[["accession", "cik", "period"]]


def holdings(dirs: list[Path], chosen: pd.DataFrame, con: duckdb.DuckDBPyConnection, keep: set[str] | None = None) -> pd.DataFrame:
    """Shares by manager, quarter and CUSIP8 from the chosen reports (SH rows without put/call). `keep`: only these CUSIP8s (a CUSIP that cannot map to a stock
    changes nothing for the others, since carry-forward and change are per CUSIP), to bound memory."""
    con.register("chosen", chosen)
    kj = ""
    if keep is not None:
        con.register("keep", pd.DataFrame({"cusip": sorted(keep)}))
        kj = "JOIN keep k ON k.cusip = upper(substr(i.CUSIP, 1, 8))"
    parts = []
    for d in dirs:
        parts.append(con.execute(f"""
            SELECT c.cik, c.period, upper(substr(i.CUSIP, 1, 8)) AS cusip, sum(TRY_CAST(i.SSHPRNAMT AS BIGINT)) AS sh
            FROM read_csv('{(d / "INFOTABLE.tsv").as_posix()}', delim='\t', header=true, all_varchar=true, quote='') i
            JOIN chosen c ON c.accession = i.ACCESSION_NUMBER {kj}
            WHERE upper(coalesce(i.SSHPRNAMTTYPE, '')) = 'SH' AND coalesce(trim(i.PUTCALL), '') = '' AND length(i.CUSIP) >= 8
            GROUP BY 1, 2, 3""").df())
    con.unregister("chosen")
    rows = pd.concat(parts, ignore_index=True)
    h = cast(pd.DataFrame, rows.groupby(["cik", "period", "cusip"], as_index=False)["sh"].sum())      # as_index=False: a frame (pandas-stubs say Series)
    return h.assign(cusip=h["cusip"].astype("category"))


def complete_quarters(h: pd.DataFrame, min_managers: int = MIN_MANAGERS) -> list[pd.Timestamp]:
    """Calendar quarter-ends reported by at least `min_managers` managers (late amendments for old quarters make small, partial periods)."""
    p = pd.to_datetime(h["period"])
    n = h.assign(period=p).groupby("period")["cik"].nunique()
    return [cast(pd.Timestamp, q) for q, k in n.items() if k >= min_managers and q == cast(pd.Timestamp, q) + pd.offsets.QuarterEnd(0)]


def with_stale_and_change(h: pd.DataFrame) -> pd.DataFrame:
    """Add the one-quarter carry-forward of managers without a report and the change against the manager's previous quarter."""
    h = h.assign(period=pd.to_datetime(h["period"]))
    qs = sorted(h["period"].unique())
    out: list[pd.DataFrame] = []
    prev: pd.DataFrame | None = None
    for q in qs:
        cur = h[h["period"] == q].assign(rdate=q)
        if prev is not None and prev["period"].iloc[0] + pd.offsets.QuarterEnd(1) != q:
            prev = None                                                        # not the adjacent quarter: nothing to carry or compare
        if prev is not None:
            gone = set(prev["cik"]) - set(cur["cik"])
            carried = prev[prev["cik"].isin(gone) & (prev["rdate"] == prev["period"])].assign(period=q)       # one quarter only
            p = prev.set_index(["cik", "cusip"])["sh"]
            cur = cur.assign(chg=cur["sh"].to_numpy() - p.reindex(pd.MultiIndex.from_frame(cur[["cik", "cusip"]])).fillna(0).to_numpy())
            cur = pd.concat([cur, carried.assign(chg=0)], ignore_index=True)
        else:
            cur = cur.assign(chg=np.nan)                                       # the first quarter has no previous one: not used as a vintage
        out.append(cur)
        prev = cur
    return pd.concat(out, ignore_index=True)


def vintages(f13_dir: Path, map_cusip: Callable[[pd.DataFrame], pd.Series], state: pd.DataFrame, min_managers: int = MIN_MANAGERS,
             keep: set[str] | None = None) -> pd.DataFrame:
    """io_pit-like rows (permno, fdate, knowledge_date, IO_COLS) for every quarter after the first in the data sets. `map_cusip` maps (cusip, event_date) rows to permnos;
    `state`: permno, smonth, shares (CRSP shares at the fdate month)."""
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='1500MB'; PRAGMA threads=2")
    dirs = [extract(z) for z in sorted(Path(f13_dir).glob("*_form13f.zip"))]
    h = holdings(dirs, select(reports(dirs, con)), con, keep)
    h = h[h["period"].isin(complete_quarters(h, min_managers))]
    h = with_stale_and_change(h)
    first = h["period"].min()
    h = h[h["period"] > first]
    keys = h[["cusip", "period"]].drop_duplicates().rename(columns={"period": "event_date"})
    keys["permno"] = map_cusip(keys).to_numpy()
    h = h.merge(keys.rename(columns={"event_date": "period"}), on=["cusip", "period"]).dropna(subset=["permno"])
    h["sh_stale"] = h["sh"].where(h["rdate"] < h["period"], 0)
    m = h.groupby(["permno", "period", "cik"], as_index=False)[["sh", "sh_stale", "chg"]].sum()              # one row per manager (panel/pit/ownership.py _mgr)
    m["sh2"], m["buy"], m["sell"] = m["sh"].astype(float) ** 2, (m["chg"] > 0).astype(int), (m["chg"] < 0).astype(int)
    v = m.groupby(["permno", "period"]).agg(n_mgr=("cik", "size"), sh=("sh", "sum"), sh2=("sh2", "sum"), sh_stale=("sh_stale", "sum"),
                                             n_buy=("buy", "sum"), n_sell=("sell", "sum"), chg=("chg", "sum")).reset_index()
    s = state.assign(smonth=pd.to_datetime(state["smonth"])).drop_duplicates(["permno", "smonth"])
    v["smonth"] = v["period"].dt.to_period("M").dt.to_timestamp()
    v = v.merge(s.rename(columns={"shares": "crsp_shares"}), on=["permno", "smonth"], how="left")
    pos = v["crsp_shares"] > 0
    out = pd.DataFrame({"permno": v["permno"].astype("int64"), "fdate": v["period"], "knowledge_date": v["period"] + pd.Timedelta(days=KNOWLEDGE_LAG), "io_n_mgr": v["n_mgr"].astype(float)})
    out["io_frac"] = (v["sh"] / v["crsp_shares"]).where(pos)
    out["io_hhi"] = (v["sh2"] / (v["sh"].astype(float) ** 2)).where(v["sh"] > 0)
    out["io_stale_frac"] = (v["sh_stale"] / v["sh"]).where(v["sh"] > 0)
    out["io_buy_sell"] = ((v["n_buy"] - v["n_sell"]) / (v["n_buy"] + v["n_sell"])).where((v["n_buy"] + v["n_sell"]) > 0)
    out["io_net_change"] = (v["chg"] / v["crsp_shares"]).where(pos)
    prev = out[["permno", "fdate", "io_frac", "io_n_mgr"]].assign(fdate=lambda x: x["fdate"] + pd.offsets.QuarterEnd(1))
    out = out.merge(prev, on=["permno", "fdate"], how="left", suffixes=("", "_p"))
    out["io_frac_chg"] = out["io_frac"] - out["io_frac_p"]
    out["io_breadth_chg"] = np.log1p(out["io_n_mgr"]) - np.log1p(out["io_n_mgr_p"])
    return out.drop(columns=["io_frac_p", "io_n_mgr_p"])


def asof(v: pd.DataFrame, permnos: pd.Series, month: pd.Timestamp) -> pd.DataFrame:
    """The engine's as-of rule: the vintage with the greatest knowledge date before `month`, kept if at most WINDOW_DAYS old."""
    p = v[v["knowledge_date"] < month].sort_values(["permno", "knowledge_date"]).drop_duplicates("permno", keep="last")
    p = p[(month - p["knowledge_date"]).dt.days <= WINDOW_DAYS]
    j = pd.DataFrame({"permno": permnos.drop_duplicates().to_numpy()}).merge(p, on="permno", how="left")
    return j.rename(columns={"knowledge_date": "kd_io"})[["permno", "kd_io", "fdate"] + IO_COLS]
