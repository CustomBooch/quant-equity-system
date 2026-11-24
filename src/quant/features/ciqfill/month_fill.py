"""November fill (lab/ciqfill/SPEC_NOV_FILL.md): the six SEC inputs of the traded model for decision month 2026-11, each behind its registered check, written as a new release.

    python -m quant.features.ciqfill.month_fill --release data/master/master_2026-11-16 --base data/derived/builds/ext_2026-10-22a --out data/master/master_2026-11-16n

Families (a family whose check fails leaves its inputs as the base release has them):
    A edgar     EDGAR counts for October (filings to 2026-09-30) and November (to 2026-10-30): the engine's events plus the form index of data/raw/SEC/sec_fill_2026-11-16
    B insider   insider inputs for November: the engine's Form 4 events (filings to 2026-09-30) plus October's original Form 4s read from EDGAR (quant.data.sec.form4)
Only empty cells of those columns in the October and November bridge rows change; every other row is copied (gate engine_months_identical). The October fill's helpers are reused
(quant.features.ciqfill.full_fill), so the definitions and checks are the same code.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from quant.core.paths import REPO
from quant.core.release import sha256_file, write_release
from quant.core.sql import day, one_row
from quant.data.sec.sec_pull import SUBS
from quant.features.ciqfill import activity_families as af
from quant.features.ciqfill import full_fill as FF

SPEC = REPO / "lab/ciqfill/SPEC_NOV_FILL.md"
SEC_FILL = REPO / "data/raw/SEC/sec_fill_2026-11-16"
OCT, NOV = pd.Timestamp("2026-10-01"), pd.Timestamp("2026-11-01")
EDGAR_END = {OCT: pd.Timestamp("2026-09-30"), NOV: pd.Timestamp("2026-10-30")}       # last filing date known before each decision month
INSIDER_END = pd.Timestamp("2026-10-30")                                            # coverage end of the November insider rule
SIX = ["ins_net_value_6m", "ins_n_buyers_6m", "ins_net_ratio_12m", "n_8k_502_365d", "n_nt_365d", "n_sc13d_365d"]


def new_edgar_events(ctx: FF.Ctx, idx: pd.DataFrame, items: pd.DataFrame, ciks: set[int]) -> tuple[pd.DataFrame, dict[str, Any], bool]:
    """Filings after the engine's extract (2026-09-15 to 2026-10-30) of the engine's CIKs, with item 5.02 and a permno; the items check (b) and its record."""
    new = idx[(idx["filingDate"] > FF.EDGAR_LOCAL_END) & (idx["filingDate"] <= EDGAR_END[NOV]) & idx["cik"].isin(ciks)].copy()
    new = new.merge(items.drop_duplicates("accession")[["accession", "items"]], on="accession", how="left")
    k8 = new["kind"] == "8k"
    share = float(new.loc[k8, "items"].notna().mean()) if k8.any() else 0.0
    new["item_502"] = new["items"].fillna("").str.contains("5.02", regex=False)
    mapped = FF.cik_to_permno(ctx.q(f"select cik, gvkey from {ctx.t('cik_gvkey')}"), ctx.q(f"select gvkey, permno, linkprim, linkdt, linkenddt from {ctx.t('gvkey_links')}"), new)
    mapped["knowledge_date"] = mapped["filingDate"] + pd.Timedelta(days=1)
    rec = {"events": len(new), "mapped_to_permno": len(mapped), "share_of_8k_with_items": round(share, 4), "by_kind": new["kind"].value_counts().to_dict(),
           "filing_dates": [str(new["filingDate"].min().date()), str(new["filingDate"].max().date())] if len(new) else None}
    return mapped, rec, share >= 0.95


def family_edgar(ctx: FF.Ctx) -> None:
    """A: the index complete where both sources exist (a), the new 8-Ks' items found (b), the combined table giving the engine's June exactly (c); then October and November."""
    rep: dict[str, Any] = {}
    idx = pd.read_parquet(SEC_FILL / "index.parquet")
    idx = idx[idx["form"].isin(FF.ENGINE_FORMS)].assign(kind=lambda x: x["form"].map(FF.ENGINE_FORMS))
    ciks = set(ctx.wk.execute("select cik from edgar_ciks").df()["cik"].astype("int64"))
    subs = ctx.q(f"""select cik, accessionNumber as accession, form, CAST(filingDate AS DATE) as filingDate from read_parquet('{SUBS.as_posix()}')
                     where filingDate >= DATE '2026-07-01' and filingDate <= {day(FF.EDGAR_LOCAL_END)}""")
    subs = subs[subs["form"].isin(FF.ENGINE_FORMS)].assign(kind=lambda x: x["form"].map(FF.ENGINE_FORMS))

    def key(d: pd.DataFrame) -> set[tuple[int, str, str]]:
        return set(zip(d["cik"].astype("int64"), d["accession"], d["kind"], strict=True))
    win = idx[(idx["filingDate"] >= pd.Timestamp("2026-07-01")) & (idx["filingDate"] <= FF.EDGAR_LOCAL_END) & idx["cik"].isin(ciks)]
    e_set, i_set = key(subs[subs["cik"].isin(ciks)]), key(win)
    comp = {"engine_events": len(e_set), "index_events": len(i_set), "engine_found_in_index": round(len(e_set & i_set) / max(len(e_set), 1), 4),
            "index_found_in_engine": round(len(e_set & i_set) / max(len(i_set), 1), 4), "threshold": FF.MIN_EQUAL}
    ok_a = ctx.gate("nov_edgar_index_complete", comp["engine_found_in_index"] >= FF.MIN_EQUAL and comp["index_found_in_engine"] >= FF.MIN_EQUAL)
    mapped, rec, items_ok = new_edgar_events(ctx, idx, pd.read_parquet(SEC_FILL / "items.parquet"), ciks)
    ok_b = ctx.gate("nov_edgar_items_found", items_ok)
    rep.update({"index_completeness_2026-07-01_to_2026-09-14": comp, "new_filings": rec})
    eng_ev = ctx.q(f"select permno, knowledge_date, kind, item_502 from {ctx.t('edgar_events')}")
    eng_ev["knowledge_date"] = pd.to_datetime(eng_ev["knowledge_date"])
    edgar_gv = set(ctx.q(f"select gvkey, cik from {ctx.t('cik_gvkey')}").pipe(lambda d: d[d["cik"].isin(ciks)])["gvkey"])
    gv = ctx.link(FF.LAST).set_index("permno")["gvkey"]
    first502 = pd.Timestamp(one_row(ctx.wk, "select item_502_first from edgar_coverage")[0])
    cols = af.EDGAR_COLS

    def counts(ev: pd.DataFrame, m: pd.Timestamp, permnos: pd.Series, last_event: pd.Timestamp) -> pd.DataFrame:
        return FF.edgar_counts(ev, gv, edgar_gv, first502, m, permnos, last_event)
    ref = ctx.engine(FF.LAST, "permno, " + ", ".join(cols)).drop_duplicates("permno").set_index("permno")
    got = counts(eng_ev, FF.LAST, pd.Series(ref.index), eng_ev["knowledge_date"].max())
    rep["replication_2026-06"] = {c: FF.equal_check(ref[c], got[c]) for c in cols}
    ok_c = ctx.gate("nov_edgar_replicates_engine", all(v["pass"] for v in rep["replication_2026-06"].values()))
    if ok_a and ok_b and ok_c:
        rep["filled_rows"] = {}
        for m in (OCT, NOV):
            part = mapped[mapped["filingDate"] <= EDGAR_END[m]]
            ev = pd.concat([eng_ev, part[["permno", "knowledge_date", "kind", "item_502"]]], ignore_index=True)
            rows = ctx.rows(m)
            rep["filled_rows"][str(m.date())] = FF.fill(ctx.b, m, counts(ev, m, rows["permno"], ev["knowledge_date"].max()), cols, "fill_edgar")
    ctx.report["families"]["A_edgar"] = rep


def form4_events(ctx: FF.Ctx) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """October's original Form 4 P/S events mapped to permnos (permno, knowledge_date, code, value) and the purchases' buyers (permno, knowledge_date, owner_cik)."""
    tx = pd.read_parquet(SEC_FILL / "form4_tx.parquet")
    tx = tx[tx["filing_date"].between(pd.Timestamp("2026-10-01"), INSIDER_END)].rename(columns={"filing_date": "filingDate"})
    mapped = FF.cik_to_permno(ctx.q(f"select cik, gvkey from {ctx.t('cik_gvkey')}"), ctx.q(f"select gvkey, permno, linkprim, linkdt, linkenddt from {ctx.t('gvkey_links')}"), tx)
    owners = pd.read_parquet(SEC_FILL / "form4_owners.parquet")
    buyers = mapped[mapped["code"] == "P"].merge(owners, on="accession")[["permno", "knowledge_date", "owner_cik"]].drop_duplicates()
    rec = {"events": len(tx), "mapped_to_permno": len(mapped), "purchases": int((mapped["code"] == "P").sum()), "sales": int((mapped["code"] == "S").sum()),
           "filing_dates": [str(tx["filingDate"].min().date()), str(tx["filingDate"].max().date())] if len(tx) else None}
    return mapped[["permno", "knowledge_date", "code", "value"]], buyers, rec


def family_insider(ctx: FF.Ctx) -> None:
    """B: November's insider inputs, if the reader reproduced the data set (check a) and October's filings were read (check b)."""
    chk = json.loads((SEC_FILL / "form4_check.json").read_text(encoding="utf-8"))
    pull = json.loads((SEC_FILL / "form4_pull.json").read_text(encoding="utf-8"))
    ok_a = ctx.gate("nov_insider_reader_matches_dataset", bool(chk.get("pass")))
    ok_b = ctx.gate("nov_insider_october_read", bool(pull.get("pass")))
    rep: dict[str, Any] = {"reader_check": chk, "october_pull": pull}
    if ok_a and ok_b:
        new_tx, new_buyers, rec = form4_events(ctx)
        rep["october_events"] = rec
        tx = pd.concat([ctx.wk.execute("select permno, knowledge_date, code, value from insider_tx").df(), new_tx], ignore_index=True)
        buyers = pd.concat([ctx.wk.execute("select permno, knowledge_date, owner_cik from insider_buyers").df(), new_buyers], ignore_index=True)
        for d in (tx, buyers):
            d["knowledge_date"] = pd.to_datetime(d["knowledge_date"])
        start = pd.Timestamp(one_row(ctx.wk, "select filing_start from insider_coverage")[0])
        rows = ctx.rows(NOV)
        gv = ctx.link(FF.LAST).drop_duplicates("permno").set_index("permno")["gvkey"]
        cik_gv = set(ctx.q(f"select gvkey from {ctx.t('cik_gvkey')}")["gvkey"])
        u = rows["permno"].drop_duplicates()
        has_cik = pd.Series([gv.get(p) in cik_gv for p in u], index=u.to_numpy())
        me = rows.drop_duplicates("permno").set_index("permno")["me_lag"]
        vals = af.insider_features(tx, buyers, (start, INSIDER_END), has_cik, me, NOV).set_index("permno")
        rep["filled_rows"] = FF.fill(ctx.b, NOV, vals, af.INSIDER_COLS, "fill_insider")
    ctx.report["families"]["B_insider"] = rep


def coverage(ctx: FF.Ctx) -> dict[str, dict[str, float]]:
    return {str(m.date())[:7]: {c: round(float(FF.investable(ctx.rows(m))[c].notna().mean()), 4) for c in SIX} for m in (OCT, NOV)}


def run(release: Path, base: Path, out: Path, allow_dirty_code: bool = False) -> dict[str, Any]:
    """Both families on the base release's bridge rows, the new release written to `out` (which must not exist); the report (also FILL_REPORT.json) is returned."""
    out.mkdir(parents=True, exist_ok=False)
    ctx = FF.Ctx(release, base, REPO / "data/raw/Capital IQ")
    ctx.report = {"spec": "lab/ciqfill/SPEC_NOV_FILL.md", "spec_sha256": sha256_file(SPEC), "families": {}}
    doc = json.loads((release / "VERSION.json").read_text(encoding="utf-8"))
    ctx.report["coverage_investable_before"] = coverage(ctx)
    for fam in (family_edgar, family_insider):
        fam(ctx)
        print(fam.__name__, "done", {k: v for k, v in ctx.gates.items() if v != "pass"}, flush=True)
    ctx.report["coverage_investable_after"] = coverage(ctx)
    FF.write(ctx, out)
    ctx.report["gates_added"] = ctx.gates
    rep = out / "FILL_REPORT.json"
    rep.write_bytes((json.dumps(ctx.report, indent=1, sort_keys=True, default=str) + "\n").encode("utf-8"))
    names = ("MANIFEST.json", "index.parquet", "items.parquet", "form4_tx.parquet", "form4_owners.parquet", "form4_check.json", "form4_pull.json")
    inputs = {"base_release_manifest": sha256_file(release / "VERSION.json"), "base_panel": sha256_file(Path(ctx.panel)),
              **{f"sec_fill/{n}": sha256_file(SEC_FILL / n) for n in names if (SEC_FILL / n).exists()}}
    gates = {**doc["validation"]["gates"], **ctx.gates}
    write_release(out, out.name, "Master panel: the November build plus the November fill (SPEC_NOV_FILL.md)", inputs=inputs,
                  validation={"report_sha256": sha256_file(rep), "gates": gates}, expected={"rows": ctx.report["rows"]}, allow_dirty_code=allow_dirty_code)
    return ctx.report


def main(argv: list[str] | None = None) -> int:
    """0 if every gate this step added passed, else 1."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--release", required=True, help="the November build (directory with master_panel.parquet and VERSION.json)")
    ap.add_argument("--base", required=True, help="engine build root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--allow-dirty-code", action="store_true")
    a = ap.parse_args(argv)
    r = run(Path(a.release), Path(a.base), Path(a.out), a.allow_dirty_code)
    bad = {k: v for k, v in r["gates_added"].items() if v != "pass"}
    print(json.dumps({"gates_failed": bad, "coverage_after": r["coverage_investable_after"]}, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
