"""Assemble the master panel: the engine panel, plus Capital IQ fundamentals where the engine has gaps, plus bridge rows for months the engine cannot build.

    python -m quant.features.ciqfill.assemble --base panel/_build/ext_2026-10-22a --ciq-dir "data/raw/Capital IQ" --out data/master/master_2026-10-23 --last-engine-month 2026-06-01

Why bridge rows: the engine's spine is built from CRSP trading, so a decision month with no CRSP data cannot exist in the engine (CRSP ends 2026-06-30, so the engine ends at 2026-06). A
Capital IQ file priced at the end of month P belongs to decision month P+1. If the engine has that month it is only filled where missing; otherwise it becomes bridge rows.
Writes master_panel.parquet, CIQ_FILL_REPORT.json and VERSION.json (release/1 with validation gates). Memory: DuckDB 3 GB, 2 threads.

Stages of `run`, in order (every check records a gate; a failed gate never stops the build, the release records it):
    Build                    read access to the engine build: one method per input, each limited to the window its rule allows
    load_estimates           the Capital IQ consensus files, each as of the first of its month
    prove_replication        on the last engine month, this module's rebuild of the engine's rules must give the engine's own values; returns the consensus features allowed to fill
    engine_fill, bridge_month   one Capital IQ fundamentals file: fill the gaps of a month the engine has, or build the rows of a month it lacks
    fill_price_features, fill_event_features, fill_sec_counts_live   free-data features of the bridge rows, each filled only if its own parity check passed
    write_panel              the engine panel with the filled and bridge rows, column types restored and 2026 outcomes sealed; then the report and the release
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any, cast

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from quant.data.capiq.ciq import load_month
from quant.features.ciqfill.ciq_est import EST_COLS, CiqEst, est_features, load_est
from quant.features.ciqfill.compustat import ADMISSIBLE_DAYS, CHECKED, PIT_COLS, asof_family, replication
from quant.features.ciqfill.features import FEATURES, fund_features
from quant.features.ciqfill import activity_families as af
from quant.features.ciqfill import pit_families as pf
from quant.features.ciqfill import event_features as ef
from quant.features.ciqfill import yahoo_price as yp
from quant.features.ciqfill.fill import bridge_rows, engine_flags, fill_month
from quant.data.capiq.ids import match_permno
from quant.features.ciqfill.retention import retention
from quant.core.sql import day, one_row
from quant.core.paths import YAHOO_DAILY
from quant.core.release import sha256_file, write_release

FUND_KEYS = {7: "july", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec"}       # capiq_fund_<key>_2026.xlsx: the export for decision month m (priced at the end of m - 1)
FIRST_FILE_MONTH, THROUGH = 7, "2026-10-01"                                        # files from July (CRSP ends with June) to the last decision month built, by default October
MIN_MATCH, MIN_PARITY, MIN_COVER = 0.98, 0.90, 0.70
# Capital IQ consensus estimates (thresholds set before any result was seen): capiq_est_<key>_2026.xlsx for month i is as of the first of that month; the files from
# April to the last decision month are required, January to March are optional extras that make the 3-month features testable
EST_KEYS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
EST_MIN_MATCH, EST_MIN_COVER = 0.95, 0.70                                         # match among companies with an estimate; coverage of investable stocks after the gap fill
EST_PARITY = {"numest_fy1": 0.90, "disp_fy1": 0.70, "rev_fy1_1m": 0.60, "rev_fy1_3m": 0.60, "numest_chg_3m": 0.60}   # Spearman against the engine's I/B/E/S values for the last engine month;
                                                                                  # a feature is filled in bridge months only if its own check was run and passed
EST_MIN_BOTH = 0.85                                                               # share of the engine's non-empty numest_fy1 that the Capital IQ build also fills
STATE_COLS = "permno, cusip, ticker, me, exch, sectype, shrtype, price, shares, pos_vol_days, siccd, delisted"
FLAG_COLS = ["in_research", "in_investable", "in_top500", "in_top1000"]
PANEL_COLS = "permno, in_investable, in_research, me_rank, in_top500, in_top1000, " + ", ".join(FEATURES + CHECKED)
SC13D_MAX_GAP_DAYS = 30                                   # the truncation BRIDGE S3 tested (n_sc13d_365d 1.000, n_8k_502_365d 0.973, n_nt_365d 0.978)
SEC_LIVE_COLS = ["n_8k_502_365d", "n_nt_365d"]          # n_sc13d_365d is not taken: the live build counts both Schedule 13D names, the engine only the old ones
# the extension step nulls every forward-return column whose window reaches past 2025-12 (2026 outcomes are unauthorised); same dict as lab.master.extend.assemble
SEAL = {"fwd_ret_1m": "2026-01-01", "mkt_ret_1m": "2026-01-01", "fwd_delist_ret_missing_1m": "2026-01-01", "fwd_ret_3m": "2025-11-01", "mkt_ret_3m": "2025-11-01",
        "fwd_delist_ret_missing_3m": "2025-11-01", "fwd_ret_6m": "2025-08-01", "mkt_ret_6m": "2025-08-01", "fwd_delist_ret_missing_6m": "2025-08-01",
        "fwd_ret_12m": "2025-02-01", "mkt_ret_12m": "2025-02-01", "fwd_delist_ret_missing_12m": "2025-02-01",
        "fwd_delist_1m": "2026-01-01", "fwd_delist_3m": "2025-11-01", "fwd_delist_6m": "2025-08-01", "fwd_delist_12m": "2025-02-01"}
INCOMPLETE = {f"fwd_incomplete_{h}m": SEAL[f"fwd_ret_{h}m"] for h in (1, 3, 6, 12)}         # must be TRUE from the seal on (the stock-level flag would reveal who stopped trading)

Gates = dict[str, str]                       # gate name -> "pass" | "fail" (the release records every gate; a failed one never stops the build)
Report = dict[str, Any]                      # CIQ_FILL_REPORT.json as it is built up (nested JSON values)
Estimates = dict[str, CiqEst]                # EST_KEYS key -> the consensus export of that month


def first_of_next_month(ts: pd.Timestamp) -> pd.Timestamp:
    """The decision month a file priced on `ts` belongs to: the first day of the following month."""
    return (ts.normalize().replace(day=1) + pd.DateOffset(months=1))


def seal_leaks(con: duckdb.DuckDBPyConnection, relation: str, columns: Collection[str]) -> tuple[dict[str, int], dict[str, int]]:
    """What breaks the seal of 2026 outcomes in `relation` (any SQL relation with a `month` column: read_parquet(...), a view), for the columns of SEAL
    and INCOMPLETE that are in `columns`: (values present at or after each column's seal month, rows at or after it whose incomplete flag is not TRUE).
    A sealed release has zeros everywhere (gate forward_returns_sealed)."""
    values = {c: int(one_row(con, f"select count({c}) from {relation} where month >= DATE '{d}'")[0]) for c, d in SEAL.items() if c in columns}
    flags = {c: int(one_row(con, f"select count(*) from {relation} where month >= DATE '{d}' and {c} is distinct from true")[0]) for c, d in INCOMPLETE.items() if c in columns}
    return values, flags


class Build:
    """Read access to one engine build (tables, security state, panel, work database). Each method returns one input for decision month `m`, limited to records
    known strictly before `m` and inside the window its rule allows. `last` is the last engine month; the identifier links of bridge months are taken from it."""

    def __init__(self, base: Path, panel: Path | None, last: pd.Timestamp) -> None:
        self.last = last
        self.panel = (panel or (base / "build/research_panel.parquet")).as_posix()
        self.state_pq = (base / "build/tables/security_state.parquet").as_posix()
        self.tables = (base / "build/tables").as_posix()
        self.con = duckdb.connect()
        self.con.execute("PRAGMA memory_limit='3GB'; PRAGMA threads=2")
        self.smax = pd.Timestamp(one_row(self.con, f"select max(smonth) from read_parquet('{self.state_pq}')")[0])
        # insider, fails-to-deliver and EDGAR inputs: the build's work database holds the insider owner list and the coverage ranges
        self.wk = duckdb.connect((base / "build/work.duckdb").as_posix(), read_only=True)
        self.cov_i = self._span(self.wk, "select filing_start, filing_end from insider_coverage")
        self.cov_f = self._span(self.wk, "select first_month, last_month from ftd_coverage")
        self.cov_e = (pd.Timestamp(one_row(self.wk, "select item_502_first from edgar_coverage")[0]),
                      pd.Timestamp(one_row(self.con, f"select max(knowledge_date) from {self.table('edgar_events')}")[0]))
        cik_pairs = self.q(f"select gvkey, cik from {self.table('cik_gvkey')}")
        self.cik_gv = set(self.q(f"select gvkey from {self.table('cik_gvkey')}")["gvkey"])
        self.edgar_gv = set(cik_pairs.loc[cik_pairs["cik"].isin(set(self.wk.execute("select cik from edgar_ciks").df()["cik"])), "gvkey"])
        self.shares_all = {sm: self.q(f"select permno, shares from read_parquet('{self.state_pq}') where smonth = {day(sm)}").drop_duplicates("permno").set_index("permno")["shares"]
                           for sm in pd.date_range(last - pd.DateOffset(months=6), self.smax, freq="MS")}

    @staticmethod
    def _span(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        """(first, last) of a one-row coverage table."""
        first, last = one_row(con, sql)
        return pd.Timestamp(first), pd.Timestamp(last)

    def q(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).df()

    def table(self, name: str) -> str:
        return f"read_parquet('{self.tables}/{name}.parquet')"

    def engine(self, m: pd.Timestamp, cols: str, investable_only: bool = False) -> pd.DataFrame:
        return self.q(f"select {cols} from read_parquet('{self.panel}') where month = {day(m)}" + (" and in_investable" if investable_only else ""))

    def state(self, m: pd.Timestamp) -> pd.DataFrame:
        return self.q(f"select {STATE_COLS} from read_parquet('{self.state_pq}') where smonth = {day(m)}")

    def link(self, m: pd.Timestamp) -> pd.DataFrame:
        return self.q(f"select permno, gvkey from {self.table('permno_gvkey')} where month = {day(m)} and gvkey is not null")

    def known(self, table: str, cols: str, m: pd.Timestamp, days: int) -> pd.DataFrame:
        """Rows of a build table with knowledge date in [m - days, m)."""
        return self.q(f"select {cols} from {self.table(table)} where knowledge_date < {day(m)} and knowledge_date >= {day(m - pd.Timedelta(days=days))}")

    def pit(self, m: pd.Timestamp) -> pd.DataFrame:
        return self.known("fund_pit", ", ".join(PIT_COLS), m, ADMISSIBLE_DAYS + 5)

    def family(self, fam: str, permnos: pd.Series, m: pd.Timestamp) -> pd.DataFrame:
        """One point-in-time family (quant.features.ciqfill.pit_families) of `permnos` as of `m`."""
        tbl, days, cols = pf.FAMILIES[fam]
        return pf.asof_permno(self.known(tbl, "permno, knowledge_date, " + ", ".join(cols), m, days + pf.SLACK_DAYS), permnos, m, fam)

    def edgar(self, m: pd.Timestamp) -> pd.DataFrame:
        return self.known("edgar_events", "permno, knowledge_date, kind, item_502", m, 451)

    def linked_to(self, permnos: pd.Series, gvkeys: set[Any]) -> pd.Series:
        """For each distinct permno, whether its gvkey in the last engine month's link is in `gvkeys`."""
        gv = self.link(self.last).drop_duplicates("permno").set_index("permno")["gvkey"]
        u = permnos.drop_duplicates()
        return pd.Series([gv.get(p) in gvkeys for p in u], index=u.to_numpy())

    def activity(self, name: str, permnos: pd.Series, me: pd.Series | None, m: pd.Timestamp, edgar_end: pd.Timestamp | None = None) -> pd.DataFrame:
        """Insider, fails-to-deliver or EDGAR features (quant.features.ciqfill.activity_families) for `permnos` in decision month `m`; `edgar_end` shortens the EDGAR coverage."""
        if name == "insider":
            if me is None:
                raise ValueError("insider features need market equity (net value is scaled by it)")
            buyers = self.wk.execute(f"select permno, knowledge_date, owner_cik from insider_buyers where knowledge_date < {day(m)} and knowledge_date >= {day(m - pd.Timedelta(days=183))}").df()
            return af.insider_features(self.known("insider_tx", "permno, knowledge_date, code, value", m, 366), buyers, self.cov_i, self.linked_to(permnos, self.cik_gv), me, m)
        if name == "edgar":
            return af.edgar_features(self.edgar(m), self.linked_to(permnos, self.edgar_gv), self.cov_e[0], edgar_end or self.cov_e[1], m)
        ftd = self.q(f"select permno, fmonth, fails_mean from {self.table('ftd_monthly')} where fmonth >= {day(m - pd.DateOffset(months=5))}")
        return af.ftd_features(ftd, self.shares_all, self.cov_f, permnos, m)


def fund_files(through: pd.Timestamp) -> tuple[str, ...]:
    """Keys of the Capital IQ fundamentals files to read, July to the decision month `through`."""
    return tuple(FUND_KEYS[m] for m in range(FIRST_FILE_MONTH, through.month + 1))


def load_estimates(ciq_dir: Path, through: pd.Timestamp) -> Estimates:
    """The consensus files capiq_est_<key>_2026.xlsx to the decision month `through` (from April required, January to March used when present). A missing,
    wrong-format or wrongly dated file raises: estimates are never silently skipped."""
    est = {}
    for k in EST_KEYS[:through.month]:
        f = ciq_dir / f"capiq_est_{k}_2026.xlsx"
        if EST_KEYS.index(k) < 3 and not f.exists():
            continue
        est[k] = load_est(f)
        want = pd.Timestamp(2026, EST_KEYS.index(k) + 1, 1)
        if est[k].asof != want:
            raise SystemExit(f"capiq_est_{k}_2026.xlsx is as of {est[k].asof.date()}, expected {want.date()}")
    return est


def prove_replication(src: Build, eng_last: pd.DataFrame, est: Estimates, gates: Gates, report: Report) -> list[str]:
    """On the last engine month, rebuilt from the state of the month before it, each of the engine's rules used for bridge rows must give the engine's own values:
    universe flags, the quarterly-history family, every point-in-time family, insider/FTD/EDGAR activity; and the Capital IQ consensus must track I/B/E/S.
    Returns the consensus features whose parity passed (none without estimates or without that state)."""
    last = src.last
    s_prev = src.state(last - pd.DateOffset(months=1))
    if not len(s_prev):
        return []
    e = eng_last.set_index("permno")
    fl = engine_flags(s_prev[s_prev["permno"].isin(e.index)]).set_index("permno")      # the engine ranks only stocks on that month's spine (they trade in the month)
    j = e.join(fl[FLAG_COLS + ["me_rank"]], rsuffix="_rule", how="inner")
    same = all((j[c].astype(bool) == j[c + "_rule"].astype(bool)).all() for c in FLAG_COLS) and np.allclose(j["me_rank"].fillna(-1), j["me_rank_rule"].fillna(-1))
    gates["flags_replicate_engine"] = "pass" if same and len(j) > 0 else "fail"
    report["flags_replication"] = {"stocks_compared": int(len(j)), "identical": bool(same)}

    me = s_prev.set_index("permno")["me"]
    rr = replication(asof_family(src.pit(last), src.link(last), last, me), e.reset_index()[["permno"] + CHECKED])      # gp_at_chg, sue_ts, earn_chg_me from fund_pit
    gates["quarterly_family_replicates_engine"] = "pass" if all(v["equal_share"] is not None and v["equal_share"] >= 0.99 and v["rebuilt_nonnull_where_engine_has"] >= 0.99 * v["engine_nonnull"] for v in rr.values()) else "fail"
    report["quarterly_family_replication"] = rr
    report["pit_family_replication"] = {}
    for fam in pf.FAMILIES:
        eng_f = src.engine(last, "permno, " + ", ".join(pf.panel_columns(fam)))
        r_f = pf.replication(src.family(fam, eng_f["permno"], last), eng_f, fam)
        gates[f"{fam}_replicates_engine"] = "pass" if pf.replicates(r_f) else "fail"
        report["pit_family_replication"][fam] = r_f
    report["activity_replication"] = {}
    for name, cols in (("insider", af.INSIDER_COLS), ("ftd", af.FTD_COLS), ("edgar", af.EDGAR_COLS)):
        eng_a = src.engine(last, "permno, " + ", ".join(cols))
        r_a = af.replication(src.activity(name, eng_a["permno"], me, last), eng_a, cols)
        no_extra = all(v["rebuilt_nonnull_total"] <= 1.01 * v["engine_nonnull"] + 5 for v in r_a.values())        # coverage must not be wider than the engine's
        gates[f"{name}_replicates_engine"] = "pass" if pf.replicates(r_a) and no_extra else "fail"
        report["activity_replication"][name] = r_a
    return estimate_parity(src, est, s_prev, gates, report) if est else []


def estimate_parity(src: Build, est: Estimates, s_prev: pd.DataFrame, gates: Gates, report: Report) -> list[str]:
    """The Capital IQ consensus build against the engine's I/B/E/S values for the last engine month (EST_PARITY thresholds; untested below 30 stocks)."""
    last = src.last
    usable: list[str] = []
    i0 = last.month - 1
    eng_e = src.engine(last, "permno, " + ", ".join(EST_PARITY))
    fe, _ = est_features(est[EST_KEYS[i0]], est.get(EST_KEYS[i0 - 1]), est.get(EST_KEYS[i0 - 3]) if i0 >= 3 else None, s_prev, last, eng_e["permno"])
    j = eng_e.merge(fe, on="permno", suffixes=("", "_c"))
    par: dict[str, dict[str, Any]] = {}
    for c, thr in EST_PARITY.items():
        b = j[[c, c + "_c"]].dropna()
        if len(b) < 30:
            par[c] = {"status": "untested (needs an earlier-month file)", "stocks": int(len(b)), "threshold": thr}
            continue
        rho = round(float(spearmanr(b[c], b[c + "_c"])[0]), 4)
        par[c] = {"stocks": int(len(b)), "spearman": rho, "threshold": thr}
        gates[f"est_parity_{c}"] = "pass" if rho >= thr else "fail"
        if rho >= thr:
            usable.append(c)
    both = float((j["numest_fy1"].notna() & j["numest_fy1_c"].notna()).sum() / max(1, j["numest_fy1"].notna().sum()))
    par["numest_fy1_share_filled_where_engine_has"] = {"share": round(both, 4), "threshold": EST_MIN_BOTH}
    gates["est_fill_share_vs_engine"] = "pass" if both >= EST_MIN_BOTH else "fail"
    report["estimates_parity"] = par
    report["estimates_filled_features"] = usable
    return usable


def investable_share(rows: pd.DataFrame, col: str) -> float:
    """Share of the investable rows with a value in `col`."""
    return float(rows.loc[rows["in_investable"].astype(bool), col].notna().mean())


def coverage(rows: pd.DataFrame, cols: Iterable[str]) -> dict[str, float]:
    """investable_share of each column, rounded to 4 decimals (as reported and as the coverage gates compare it)."""
    return {c: round(investable_share(rows, c), 4) for c in cols}


def engine_fill(src: Build, decision: pd.Timestamp, fu: pd.DataFrame, info: dict[str, Any], gates: Gates) -> pd.DataFrame:
    """A month the engine has: Capital IQ fundamentals fill only its empty cells (gate: no engine value overwritten)."""
    eng_full = src.engine(decision, "*")
    filled, stats = fill_month(eng_full, fu[["permno"] + FEATURES])
    kept = all((filled[c][~eng_full[c].isna()] == eng_full[c][~eng_full[c].isna()]).all() for c in FEATURES)
    gates["no_engine_value_overwritten"] = "pass" if kept else "fail"
    info["fill"] = stats
    return filled


def bridge_month(src: Build, key: str, rows: pd.DataFrame, state: pd.DataFrame, decision: pd.Timestamp, est: Estimates, usable: list[str], ref_investable: int,
                 info: dict[str, Any], gates: Gates, report: Report) -> pd.DataFrame:
    """A month the engine lacks: to the `rows` of quant.features.ciqfill.fill.bridge_rows add the quarterly family, every point-in-time family, insider/FTD/EDGAR activity and,
    with estimates, the Capital IQ consensus where I/B/E/S has none; then the month's coverage and universe-size gates."""
    rows = rows.merge(asof_family(src.pit(decision), src.link(src.last), decision, rows.set_index("permno")["me_lag"]), on="permno", how="left")
    fam_cov: dict[str, float] = {}
    for name in pf.FAMILIES:
        rows = rows.merge(src.family(name, rows["permno"], decision), on="permno", how="left")
        first = pf.panel_columns(name)[0]
        fam_cov[name] = coverage(rows, [first])[first]
    rows = rows.merge(src.activity("insider", rows["permno"], rows.set_index("permno")["me_lag"], decision), on="permno", how="left") \
               .merge(src.activity("ftd", rows["permno"], None, decision), on="permno", how="left").merge(edgar_counts(src, rows["permno"], decision, report), on="permno", how="left")
    if est:
        consensus_fill(rows, est, EST_KEYS[decision.month - 1], state, decision, usable, info, gates, key)       # the estimates as of the same decision month
    info["coverage_activity"] = coverage(rows, af.INSIDER_COLS + af.FTD_COLS + af.EDGAR_COLS)
    info["coverage_pit_families"] = fam_cov                  # informational: outside a family's admissible window it is empty by design
    info["coverage_quarterly_family"] = cov_q = coverage(rows, CHECKED)
    gates[f"coverage_quarterly_{key}"] = "pass" if all(v >= MIN_COVER for v in cov_q.values()) else "fail"
    n_inv = int(rows["in_investable"].astype(bool).sum())
    info.update({"bridge_rows": int(len(rows)), "bridge_investable": n_inv, "market_source": rows["bridge_market_source"].iloc[0] if len(rows) else None,
                 "rows_with_capital_iq_match": int(rows["ciq_matched"].sum())})
    gates[f"universe_size_{key}"] = "pass" if 0.85 * ref_investable <= n_inv <= 1.15 * ref_investable else "fail"
    return rows


def edgar_counts(src: Build, permnos: pd.Series, decision: pd.Timestamp, report: Report) -> pd.DataFrame:
    """EDGAR counts for a bridge month. When the EDGAR table ends inside the 365-day window by at most SC13D_MAX_GAP_DAYS, n_sc13d_365d is counted on the truncated window
    (BRIDGE S3 30-day truncation check: rank correlation 1.000) and flagged."""
    edg = src.activity("edgar", permnos, None, decision)
    gap = (decision - src.cov_e[1]).days
    if edg["n_sc13d_365d"].isna().all() and gap <= SC13D_MAX_GAP_DAYS:
        edg["n_sc13d_365d"] = src.activity("edgar", permnos, None, decision, edgar_end=decision - pd.Timedelta(days=2))["n_sc13d_365d"].to_numpy()
        report.setdefault("sc13d_truncated", {})[str(decision.date())] = {"days_missing_from_window": gap, "filled": int(edg["n_sc13d_365d"].notna().sum())}
    edg["sc13d_truncated"] = bool(gap > 2 and gap <= SC13D_MAX_GAP_DAYS)
    return edg


def consensus_fill(rows: pd.DataFrame, est: Estimates, ck: str, state: pd.DataFrame, decision: pd.Timestamp, usable: list[str], info: dict[str, Any], gates: Gates, key: str) -> None:
    """Capital IQ consensus (snapshot `ck`) fills, in place, only the rows with no I/B/E/S consensus in window, and only the features whose parity passed; never overwrites."""
    i = EST_KEYS.index(ck)
    if est[ck].asof != decision:                                   # an explicit check, not an assert: it must also hold under python -O
        raise SystemExit(f"estimate snapshot {ck} is as of {est[ck].asof.date()}, expected {decision.date()}")
    fe, es = est_features(est[ck], est[EST_KEYS[i - 1]], est.get(EST_KEYS[i - 3]), state, decision, rows["permno"])
    need = rows["kd_ibes_cons"].isna().to_numpy()
    fe = fe.set_index("permno").reindex(rows["permno"]).reset_index()
    for c in [c for c in EST_COLS if c in usable]:
        rows.loc[need, c] = fe.loc[need, c].to_numpy()
    got = need & (fe[[c for c in EST_COLS if c in usable]].notna().any(axis=1).to_numpy() if usable else False)
    rows["ibes_cons_ciq"] = got
    rows["ciq_est_same_day"] = got & fe["same_day"].to_numpy()
    after = coverage(rows, ["numest_fy1", "rev_fy1_3m"])
    es.update({"filled_rows": int(got.sum()), "rows_without_ibes": int(need.sum()), "same_day_rows": int(rows["ciq_est_same_day"].sum()),
               "coverage_numest_fy1_after_fill": after["numest_fy1"], "coverage_rev_fy1_3m_after_fill": after["rev_fy1_3m"]})
    info["estimates"] = es
    gates[f"est_match_{key}"] = "pass" if es["match_rate"] is not None and es["match_rate"] >= EST_MIN_MATCH else "fail"
    gates[f"est_coverage_{key}"] = "pass" if es["coverage_numest_fy1_after_fill"] >= EST_MIN_COVER else "fail"


def fill_price_features(src: Build, yahoo_dir: Path, bridge_parts: list[pd.DataFrame], gates: Gates, report: Report) -> tuple[str, pd.DataFrame, pd.Series]:
    """Compute the Yahoo-built price features for the bridge decision months, check them against the engine's last three months, and fill bridge rows (in place) with the
    features that pass. Returns the digest of the Yahoo files and the daily bars and market proxy (which the event features reuse)."""
    last = src.last
    ids = sorted(int(f.name.split(".")[0]) for f in Path(yahoo_dir).glob("*.json.gz"))
    daily = yp.load_yahoo(ids, Path(yahoo_dir))
    start = last - pd.DateOffset(months=24)
    st = src.q(f"select smonth, permno, me, shares from read_parquet('{src.state_pq}') where smonth >= {day(start)}").drop_duplicates(["smonth", "permno"])
    st["p"] = pd.to_datetime(st["smonth"]).dt.to_period("M")
    me = {cast(pd.Period, p): g.set_index("permno")["me"] for p, g in st.groupby("p")}
    shares = st.pivot(index="p", columns="permno", values="shares")
    mkt = yp.market_proxy(daily, me)
    proxy: dict[str, Any] = {"stocks_with_bars": int(daily["id"].nunique()), "days": int(len(mkt))}
    if one_row(src.wk, "select count(*) from information_schema.tables where table_name = 'crsp_daily'")[0]:
        cr = src.wk.execute(f"select date, max(vwretd) as x from crsp_daily where date >= {day(start)} group by date").df()
        j = pd.concat([mkt.rename("proxy"), pd.Series(cr["x"].to_numpy(), index=pd.to_datetime(cr["date"])).rename("crsp")], axis=1).dropna()
        proxy.update({"days_compared": int(len(j)), "daily_correlation_with_crsp_vwretd": round(float(cast(float, j.corr().iloc[0, 1])), 5), "threshold": yp.MIN_PROXY_CORR})
        gates["px_market_proxy"] = "pass" if proxy["daily_correlation_with_crsp_vwretd"] >= yp.MIN_PROXY_CORR else "fail"
    else:
        proxy["note"] = "not checked: no crsp_daily table in the build database"
    report["px_market_proxy"] = proxy
    # shares for months after the last state month come from the bridge rows (the decision month after p carries the share count at the end of p)
    for rows in bridge_parts:
        p = pd.Timestamp(rows["month"].iloc[0]).to_period("M") - 1
        if p not in shares.index:
            shares.loc[p] = np.nan
        shares.loc[p, rows["permno"].to_numpy()] = rows["shares_lag"].to_numpy()
    shares = shares.sort_index()
    rt = src.q(f"select permno, month, ret_1m from read_parquet('{src.panel}') where month >= {day(last - pd.DateOffset(months=60))} and month <= {day(last)}")
    rt["p"] = pd.to_datetime(rt["month"]).dt.to_period("M") - 1                   # type: ignore[operator]  # the month before; pandas-stubs lack Period-series arithmetic
    ret_total = rt.pivot(index="p", columns="permno", values="ret_1m")
    pmonths = [str((last - pd.DateOffset(months=k)).to_period("M")) for k in (2, 1, 0)]
    bmonths = [str(pd.Timestamp(r["month"].iloc[0]).to_period("M")) for r in bridge_parts]
    built = yp.price_features(daily, mkt, shares, pmonths + bmonths, ret_total)
    eng = {m: src.engine(pd.Timestamp(f"{m}-01"), "permno, " + ", ".join(yp.FEATURES), investable_only=True).set_index("permno") for m in pmonths}
    par = yp.parity(built, eng)
    usable: list[str] = []
    for c, v in par.items():
        gates[f"px_parity_{c}"] = "pass" if v["pass"] else "fail"
        if v["pass"] and c not in yp.FILL_SKIP:
            usable.append(c)
    report["px_parity"] = par
    report["px_filled_features"] = usable
    for rows, m in zip(bridge_parts, bmonths, strict=True):
        fe = built[m].reindex(rows["permno"])
        for c in usable:
            rows[c] = fe[c].to_numpy(dtype=float)
        rows["px_yahoo"] = fe[usable].notna().any(axis=1).to_numpy()
        cov = investable_share(rows, "ret_1m")
        gates[f"px_coverage_{m}"] = "pass" if cov >= yp.MIN_COVERAGE else "fail"                      # the gate compares the unrounded share; the report rounds it
        report.setdefault("px_months", {})[m] = {"rows_with_price_features": int(rows["px_yahoo"].sum()), "investable_coverage_ret_1m": round(cov, 4)}
    digest = hashlib.sha256(("\n").join(f"{f.name} {f.stat().st_size}" for f in sorted(Path(yahoo_dir).glob("*.json.gz"))).encode()).hexdigest()
    return digest, daily, mkt


def fill_event_features(src: Build, daily: pd.DataFrame, mkt: pd.Series, bridge_parts: list[pd.DataFrame], gates: Gates, report: Report) -> None:
    """Announcement-window features (ear_rdq, ear_ann, abn_vol_ann, post_ann_car) from Yahoo bars and Compustat report dates; parity against the engine for its last three months, fill what passes."""
    last = src.last

    def events(m: pd.Timestamp) -> pd.DataFrame:
        """Compustat report dates of the last 200 days before `m`, mapped to permnos by the link of month `m`."""
        return src.q(f"select gvkey, rdq as event_date from {src.table('fund_q')} where rdq >= {day(m - pd.Timedelta(days=200))} and rdq is not null").drop_duplicates() \
            .merge(src.link(m).drop_duplicates("gvkey"), on="gvkey")[["permno", "event_date"]]
    built: dict[str, pd.DataFrame] = {}
    eng: dict[str, pd.DataFrame] = {}
    S = ef._series(daily, mkt)                                     # month-independent: computed once for the parity months and the bridge months
    for m in [last - pd.DateOffset(months=k) for k in (2, 1, 0)]:
        e = src.engine(m, "permno, " + ", ".join(ef.COLS + ef.FILL_IF_EMPTY + ef.KD_COLS), investable_only=True).set_index("permno")
        built[str(m.to_period("M"))] = ef.event_features(daily, mkt, events(m), pd.Series(e.index), m, S=S).set_index("permno")
        eng[str(m.to_period("M"))] = e
    par = yp.parity(built, eng, ef.COLS + ef.FILL_IF_EMPTY)
    gap_fill = [c for c in ef.FILL_IF_EMPTY if par[c]["pass"]]
    usable = [c for c in ef.COLS if par[c]["pass"]]
    for c, v in par.items():
        gates[f"ev_parity_{c}"] = "pass" if v["pass"] else "fail"
    report["ev_parity"], report["ev_filled_features"], report["ev_filled_where_empty"] = par, usable, gap_fill
    ev_all = events(last)
    resp_all = ef.responses(daily, mkt, ev_all, S)                 # the same events for every bridge month
    for rows in bridge_parts:
        d = pd.Timestamp(rows["month"].iloc[0])
        fe = ef.event_features(daily, mkt, ev_all, rows["permno"], d, S=S, resp=resp_all).set_index("permno").reindex(rows["permno"])
        for c in usable:
            rows[c] = fe[c].to_numpy(dtype=float)
        if usable:
            rows["kd_ear_rdq"] = fe["kd_ear_rdq"].to_numpy()
            rows["kd_ear_ann"] = fe["kd_ear_ann"].to_numpy()
        rows["ann_events_proxy"] = fe[[c for c in usable if c != "ear_rdq"]].notna().any(axis=1).to_numpy() if usable else False
        n_gap: dict[str, int] = {}
        for c in gap_fill:
            if c not in rows:
                rows[c] = np.nan
            empty = rows[c].isna().to_numpy() & fe[c].notna().to_numpy()
            rows.loc[empty, c] = fe[c].to_numpy(dtype=float)[empty]
            n_gap[c] = int(empty.sum())
        cov = investable_share(rows, "ear_rdq") if "ear_rdq" in rows else 0.0
        gates[f"ev_coverage_{str(d.to_period('M'))}"] = "pass" if cov >= yp.MIN_COVERAGE else "fail"
        report.setdefault("ev_months", {})[str(d.to_period("M"))] = {"investable_coverage_ear_rdq": round(cov, 4), "rows_with_ann_proxy": int(rows["ann_events_proxy"].sum()),
                                                                    "rows_filled_where_ibes_empty": n_gap}


def fill_sec_counts_live(events_asof: Path, decision: pd.Timestamp, bridge_parts: list[pd.DataFrame], gates: Gates, report: Report) -> None:
    """SEC filing counts for a bridge month whose window the engine's EDGAR table does not reach, from the BRIDGE live builder (quant.research.bridge.live_events: the engine's definitions, exact
    replication of the panel 2025-10..2026-06 in BRIDGE S3; filings to `decision`). Only empty cells are filled. Gate (BRIDGE rule, fixed before the result): coverage of the month's
    investable rows at least 70% and within-month rank correlation with the previous bridge month's engine-rule values at least 0.90 (365-day counts move little in a month)."""
    by_m = {pd.Timestamp(r["month"].iloc[0]): r for r in bridge_parts}
    if decision not in by_m or not events_asof.exists():
        report["sec_live"] = {"note": "not applied: no bridge rows for the month or no live file"}
        return
    live = pd.read_parquet(events_asof)
    rows, prev = by_m[decision], by_m.get(decision - pd.DateOffset(months=1))
    inv = rows["in_investable"].astype(bool).to_numpy()
    res: dict[str, Any] = {"file": str(events_asof), "decision_month": str(decision.date()), "features": {}}
    for c in SEC_LIVE_COLS:
        v = live[c].reindex(rows["permno"].to_numpy()).to_numpy(dtype=float)
        empty = rows[c].isna().to_numpy() if c in rows else np.ones(len(rows), bool)
        cov = float((~np.isnan(v[inv])).mean()) if inv.any() else 0.0
        rho: float | None = None
        if prev is not None and c in prev:
            j = pd.DataFrame({"now": pd.Series(v, index=rows["permno"].to_numpy())}).join(prev.set_index("permno")[c].rename("prev")).dropna()
            rho = float(j["now"].corr(j["prev"], method="spearman")) if len(j) >= 30 else None
        ok = bool(cov >= yp.MIN_COVERAGE and rho is not None and rho >= yp.MIN_RANK_CORR)
        gates[f"sec_live_{c}"] = "pass" if ok else "fail"
        if ok:
            rows.loc[empty, c] = v[empty]
        res["features"][c] = {"coverage_investable": round(cov, 4), "rank_corr_with_previous_month": round(rho, 4) if rho is not None else None, "filled_rows": int(empty.sum()) if ok else 0}
    rows["sec_counts_live"] = any(gates[f"sec_live_{c}"] == "pass" for c in SEC_LIVE_COLS)
    report["sec_live"] = res


def write_panel(src: Build, target: str, engine_parts: list[pd.DataFrame], bridge_parts: list[pd.DataFrame], replaced: set[pd.Timestamp], gates: Gates, report: Report) -> None:
    """The engine panel without the months that were filled, plus the filled months and the bridge rows, ordered by month and permno, with the base column types
    restored and the 2026 outcomes sealed (SEAL, INCOMPLETE) whatever panel it extends; then the gates on what was written."""
    con, panel_pq = src.con, src.panel
    flags_sql = ", ".join(f"false AS {c}_ciq" for c in FEATURES)
    key_types = {r[0]: r[1] for r in con.execute(f"describe select permno, month from read_parquet('{panel_pq}')").fetchall()}
    cast = f"REPLACE (CAST(permno AS {key_types['permno']}) AS permno, CAST(month AS {key_types['month']}) AS month)"      # keep the base column types
    skip = ", ".join(day(m) for m in sorted(replaced)) or "DATE '1900-01-01'"
    unions = ""
    if engine_parts:
        con.register("engine_all", pd.concat(engine_parts, ignore_index=True))
        unions += f" UNION ALL BY NAME SELECT * {cast} FROM engine_all"
    if bridge_parts:
        con.register("bridge_all", pd.concat(bridge_parts, ignore_index=True))
        unions += f" UNION ALL BY NAME SELECT * {cast} FROM bridge_all"
    inner = f"SELECT *, false AS bridge, {flags_sql} FROM read_parquet('{panel_pq}') WHERE month NOT IN ({skip}){unions}"
    base_types = {r[0]: r[1] for r in con.execute(f"describe select * from read_parquet('{panel_pq}')").fetchall()}
    drift = {r[0]: base_types[r[0]] for r in con.execute(f"describe {inner}").fetchall() if r[0] in base_types and r[1] != base_types[r[0]]}      # pandas frames turn dates into timestamps and integer counts into doubles
    expr = {c: f"CAST({c} AS {ty})" for c, ty in drift.items()}
    for c, d in SEAL.items():                                                         # the release is sealed whatever panel it extends (the gate below checks it)
        if c in base_types:
            expr[c] = f"CASE WHEN month >= DATE '{d}' THEN NULL ELSE {expr.get(c, c)} END"
    for c, d in INCOMPLETE.items():
        if c in base_types:
            expr[c] = f"CASE WHEN month >= DATE '{d}' THEN TRUE ELSE {expr.get(c, c)} END"
    fix = f" REPLACE ({', '.join(f'{e} AS {c}' for c, e in expr.items())})" if expr else ""
    con.execute(f"""COPY (SELECT *{fix} FROM ({inner}) ORDER BY month, permno) TO '{target}' (FORMAT PARQUET)""")
    report["column_types_restored_to_base"] = sorted(drift)
    n_base = one_row(con, f"select count(*) from read_parquet('{panel_pq}') where month NOT IN ({skip})")[0]
    n_kept = one_row(con, f"select count(*) from read_parquet('{target}') where not bridge and month NOT IN ({skip})")[0]
    gates["engine_months_intact"] = "pass" if n_base == n_kept else "fail"
    out_types = {r[0]: r[1] for r in con.execute(f"describe select * from read_parquet('{target}')").fetchall()}
    gates["column_types_match_base"] = "pass" if all(out_types.get(c) == ty for c, ty in base_types.items()) else "fail"
    values, flags = seal_leaks(con, f"read_parquet('{target}')", out_types)
    leaked = {**values, **flags}
    gates["forward_returns_sealed"] = "pass" if all(v == 0 for v in leaked.values()) else "fail"
    report["sealed_columns_checked"] = len(leaked)
    report["rows"] = {"engine_rows_kept": int(n_kept), "engine_rows_filled": int(sum(len(p) for p in engine_parts)), "bridge_rows": int(sum(len(p) for p in bridge_parts))}


def run(base: Path, ciq_dir: Path, out: Path, last_engine_month: pd.Timestamp, panel: Path | None = None, estimates: bool = True, yahoo_dir: Path | None = None,
        sec_live: Path | None = None, sec_live_month: str = "2026-10-01", allow_dirty_code: bool = False, through: str = THROUGH) -> Report:
    """`base`: engine build root (tables, work database). `panel`: the panel to extend, default the build's own research_panel.parquet; pass the sealed extended master to keep its extra columns.
    `through`: the last decision month built; the Capital IQ files of every month from July to it must be in `ciq_dir`. Returns the report (also written as
    CIQ_FILL_REPORT.json beside the panel and the release manifest). SystemExit if `out` exists, the base panel has no rows for the last engine month, or an
    estimates file is wrongly dated; CiqFormatError for a fundamentals export of the wrong layout."""
    through_month = pd.Timestamp(through)
    out.mkdir(parents=True, exist_ok=False)
    src = Build(base, panel, last_engine_month)
    eng_last = src.engine(last_engine_month, PANEL_COLS)
    if eng_last.empty:
        raise SystemExit(f"the base panel has no rows for {last_engine_month.date()}")
    report: Report = {"last_engine_month": str(last_engine_month.date()), "months": {}}
    gates: Gates = {}
    est = load_estimates(ciq_dir, through_month) if estimates else {}
    usable = prove_replication(src, eng_last, est, gates, report)          # consensus features whose parity check ran and passed (filled in bridge months)

    engine_parts: list[pd.DataFrame] = []
    bridge_parts: list[pd.DataFrame] = []
    replaced: set[pd.Timestamp] = set()
    prev_inv = {int(p) for p in eng_last.loc[eng_last["in_investable"].astype(bool), "permno"]}      # universe retention: last month's investable stocks that must still have a row
    files = fund_files(through_month)
    for key in files:
        path = ciq_dir / f"capiq_fund_{key}_2026.xlsx"
        cm = load_month(path)                                  # raises CiqFormatError on the old or a broken format
        decision = first_of_next_month(cm.pricing_date)
        engine_case = decision <= last_engine_month
        st_month = decision - pd.DateOffset(months=1) if engine_case else src.smax
        state = src.state(st_month)
        t = match_permno(cm.table, state.assign(cusip=state["cusip"]))
        feats = fund_features(t)
        has_cap = t["mcap"].notna()
        rate = float(t.loc[has_cap, "permno"].notna().mean())
        info: dict[str, Any] = {"file": path.name, "pricing_date": str(cm.pricing_date.date()), "asof_date": str(cm.asof_date.date()), "decision_month": str(decision.date()),
                "kind": "engine_fill" if engine_case else "bridge", "stocks_with_cap": int(has_cap.sum()), "match_rate": round(rate, 4),
                "late_filings": int(t.loc[has_cap, "late_filing"].sum()), "state_month": str(st_month.date())}
        gates[f"match_{key}"] = "pass" if rate >= MIN_MATCH else "fail"
        fu = pd.concat([t[["permno", "mcap"]], feats], axis=1).dropna(subset=["permno"]).sort_values("mcap", ascending=False).drop_duplicates("permno")
        if key == files[0]:                                    # parity against the engine: same month if the engine has it, else the last engine month (one month apart)
            ref = src.engine(decision, PANEL_COLS) if engine_case else eng_last
            both = ref[["permno", "in_investable"] + FEATURES].merge(fu[["permno"] + FEATURES], on="permno", suffixes=("", "_ciq"))
            par = {c: (round(float(spearmanr(*both[[c, c + "_ciq"]].dropna().values.T)[0]), 4) if both[[c, c + "_ciq"]].dropna().shape[0] > 30 else None) for c in FEATURES}
            info["parity_vs_engine"] = {"reference_month": str((decision if engine_case else last_engine_month).date()), "spearman": par}
            gates["parity_vs_engine"] = "pass" if all(v is not None and v >= MIN_PARITY for v in par.values()) else "fail"
        if engine_case:
            rows = engine_fill(src, decision, fu, info, gates)
            engine_parts.append(rows)
            replaced.add(decision)
        else:
            rows = bridge_rows(t, feats, engine_flags(state), decision, exact_state=(cm.pricing_date.normalize().replace(day=1) == st_month))
            rows = bridge_month(src, key, rows, state, decision, est, usable, int(eng_last["in_investable"].astype(bool).sum()), info, gates, report)
            bridge_parts.append(rows)
            ret = retention(prev_inv, set(rows["permno"].astype(int)))
            report.setdefault("retention", {})[key] = ret
            gates[f"retention_{key}"] = ret["result"]
            prev_inv = {int(p) for p in rows.loc[rows["in_investable"].astype(bool), "permno"]}
        info["coverage_of_investable"] = cov = coverage(rows, FEATURES)
        gates[f"coverage_{key}"] = "pass" if all(v >= MIN_COVER for v in cov.values()) else "fail"
        report["months"][key] = info

    yahoo_digest = None
    if yahoo_dir is not None and bridge_parts:                             # price and volume features from the free Yahoo bars, each gated against the engine's CRSP-built values
        yahoo_digest, daily, mkt = fill_price_features(src, yahoo_dir, bridge_parts, gates, report)
        fill_event_features(src, daily, mkt, bridge_parts, gates, report)
    if sec_live is not None:
        fill_sec_counts_live(sec_live, pd.Timestamp(sec_live_month), bridge_parts, gates, report)
        for r in bridge_parts:
            if "sec_counts_live" not in r:
                r["sec_counts_live"] = False

    write_panel(src, (out / "master_panel.parquet").as_posix(), engine_parts, bridge_parts, replaced, gates, report)
    report["gates"] = gates
    rep = out / "CIQ_FILL_REPORT.json"
    rep.write_bytes((json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    inputs = {**({"yahoo_files_digest": yahoo_digest} if yahoo_digest else {}), **({"sec_live_events": sha256_file(sec_live)} if sec_live is not None and sec_live.exists() else {}), "base_panel": sha256_file(Path(src.panel)),
              **{p.name: sha256_file(p) for p in sorted([*ciq_dir.glob("capiq_fund_*_2026.xlsx"), *(ciq_dir.glob("capiq_est_*_2026.xlsx") if estimates else [])])}}
    write_release(out, out.name, "Master panel: engine panel + Capital IQ fundamentals (fill and bridge rows)", inputs=inputs,
                  validation={"report_sha256": sha256_file(rep), "gates": gates}, expected={"rows": report["rows"]}, allow_dirty_code=allow_dirty_code)
    return report


def main(argv: list[str] | None = None) -> int:
    """Build a master release from the command line; 0 if every gate passed, else 1 (the release is written either way)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="engine build root (contains build/research_panel.parquet)")
    ap.add_argument("--ciq-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--last-engine-month", required=True, help="last decision month the engine built, YYYY-MM-01")
    ap.add_argument("--yahoo-dir", default=str(YAHOO_DAILY), help="Yahoo daily bars, one <permno>.json.gz per stock (price features for the bridge months)")
    ap.add_argument("--no-yahoo", action="store_true", help="skip the Yahoo price features")
    ap.add_argument("--no-estimates", action="store_true", help="skip the Capital IQ consensus estimates (files capiq_est_apr..oct_2026.xlsx are otherwise required)")
    ap.add_argument("--sec-live", default=None, help="BRIDGE live SEC events (events_asof.parquet) for --sec-live-month, used only where the engine's EDGAR window does not reach")
    ap.add_argument("--sec-live-month", default="2026-10-01")
    ap.add_argument("--allow-dirty-code", action="store_true", help="build although code files have uncommitted changes; the release then records code_dirty and the state hash")
    ap.add_argument("--panel", default=None, help="panel parquet to extend (default: the build's research_panel.parquet); use the sealed extended master")
    ap.add_argument("--through", default=THROUGH, help="last decision month to build, YYYY-MM-01 (the Capital IQ files from July to it must be present)")
    a = ap.parse_args(argv)
    rep = run(Path(a.base), Path(a.ciq_dir), Path(a.out), pd.Timestamp(a.last_engine_month), Path(a.panel) if a.panel else None, estimates=not a.no_estimates, yahoo_dir=None if a.no_yahoo else Path(a.yahoo_dir),
              sec_live=Path(a.sec_live) if a.sec_live else None, sec_live_month=a.sec_live_month, allow_dirty_code=a.allow_dirty_code, through=a.through)
    print(json.dumps({"rows": rep["rows"], "gates": rep["gates"]}, indent=1))
    return 0 if all(v == "pass" for v in rep["gates"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
