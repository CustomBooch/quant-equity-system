"""Synthetic Capital IQ exports, engine builds, Yahoo bars and states for the ciqfill tests (no licensed data). Each factory builds files in a temporary folder."""
from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import duckdb
import numpy as np
import pandas as pd

from quant.features.ciqfill import activity_families as af
from quant.features.ciqfill import event_features as ef
from quant.features.ciqfill import pit_families as pf
from quant.features.ciqfill import yahoo_price as yp
from quant.features.ciqfill.assemble import run as _run
from quant.data.capiq.ciq import load_month
from quant.features.ciqfill.ciq_est import EST_COLS, est_features, load_est
from quant.features.ciqfill.features import FEATURES, fund_features
from quant.features.ciqfill.fill import engine_flags


def run(*args, **kwargs):
    """The tests build releases in temporary folders; they are not about provenance, so the uncommitted-code refusal of the release writer is switched off here."""
    kwargs.setdefault("allow_dirty_code", True)
    return _run(*args, **kwargs)


COLS = ["Entity Name", "Entity ID", "Issue CUSIP", "Ticker", "Total Revenue", "Cost Of Goods Sold", "Gross Profit", "Total Assets", "Total Common Equity", "Cash from Ops.",
        "Total Debt", "Net Income", "Financial Filing Date", "Market Capitalization", "Day Close Price", "Shares Outstanding"]

MNEM = ["SP_ENTITY_NAME", "SP_ENTITY_ID", "SP_CUSIP", "SP_TICKER", "IQ_TOTAL_REV", "IQ_COGS", "IQ_GP", "IQ_TOTAL_ASSETS", "IQ_TOTAL_COMMON_EQUITY", "IQ_CASH_OPER", "IQ_TOTAL_DEBT",
        "IQ_NET_INC_PARENT", "IQ_FINL_FILING_DATE", "SP_MARKETCAP", "SP_PRICE_CLOSE", "SP_SHARES_OUT"]


def SERIAL(d: str | pd.Timestamp) -> int:
    """Excel serial day number of a date."""
    return (pd.Timestamp(d) - pd.Timestamp("1899-12-30")).days


def col_letter(i):
    return "ABCDEFGHIJKLMNOP"[i]


def write_xlsx(path: Path, rows: list[list]):
    def cell(r, c, v):
        if v is None:
            return ""
        ref = f"{col_letter(c)}{r}"
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
    sheet = "".join(f'<row r="{r}">' + "".join(cell(r, c, v) for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows, 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{sheet}</sheetData></worksheet>')


def company_rows(n=60, seed=0, filed="2026-05-01", exch="NYSE"):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        mcap = float(rng.uniform(300, 20000))          # $M
        price = float(rng.uniform(8, 120))
        rev = float(rng.uniform(5e4, 5e6))             # $000
        out.append([f"Company {i:02d} Inc. ({exch}:T{i:02d})", 1000 + i, f"C{i:07d}0", f"T{i:02d}", rev, rev * rng.uniform(0.3, 0.9), None, float(rng.uniform(1e5, 1e7)),
                    float(rng.uniform(1e4, 5e6)), float(rng.normal(2e4, 1e5)), float(rng.uniform(0, 2e6)), float(rng.normal(1e4, 8e4)), SERIAL(filed), mcap, price, mcap * 1e6 / price])
    return out


def header(pricing="06/30/2026", asof="07/01/2026", period="Last Twelve Months", mnem=None):
    flow = {4, 5, 6, 7, 8, 9, 10, 11, 12}
    r_mn = list(mnem or MNEM)
    r_pe = [None] * 4 + [period if i in flow else pricing for i in range(4, 16)]
    r_as = [None] * 4 + [asof if i in {4, 5, 6, 7, 8, 9, 10, 11, 12} else None for i in range(4, 16)]
    return [[None] * 16, [None] * 16, [c + ("\n($000)" if 4 <= i <= 11 else "") for i, c in enumerate(COLS)], r_mn, r_pe, r_as]


def make_file(path, rows=None, **kw):
    write_xlsx(path, header(**kw) + (rows if rows is not None else company_rows()))
    return path


def make_state(n=60, seed=3, smonth="2026-06-01"):
    """A synthetic security_state month whose market equity follows the Capital IQ July file (same seed) with a little noise."""
    rng = np.random.default_rng(seed)
    rows = company_rows(n, seed=10)
    return pd.DataFrame({"permno": [100 + i for i in range(n)], "cusip": [f"C{i:07d}" for i in range(n)], "ticker": [f"T{i:02d}" for i in range(n)],
                         "me": [r[13] * 1e6 * (1 + rng.normal(0, 0.01)) for r in rows], "price": [r[14] for r in rows], "shares": [r[15] for r in rows],
                         "exch": ["N" if i < 40 else "Q" for i in range(n)], "sectype": "EQTY", "shrtype": "NS", "pos_vol_days": 20, "siccd": 3571, "delisted": False,
                         "smonth": pd.Timestamp(smonth).date()})


EST_MNEM = ["SP_ENTITY_NAME", "SP_ENTITY_ID", "SP_CUSIP", "SP_TICKER", "SP_EPS_EST", "SP_EPS_MEDIAN_EST", "SP_EPS_HIGH_EST", "SP_EPS_LOW_EST", "SP_EPS_STDDEV_EST", "SP_EPS_NUM_EST", "SP_EPS_DATE_OF_EST",
            "SP_EST_EPS_DIFF", "SP_EST_EPS_SURPRISE_PERCENT"]

EST_DISPLAY = ["Entity Name", "Entity ID", "Issue CUSIP", "Ticker", "CIQ EPS Est\n($)", "CIQ EPS Est Med\n($)", "CIQ EPS Est High\n($)", "CIQ EPS Est Low\n($)",
               "CIQ EPS Est - Std Dev\n($)", "CIQ EPS Est - # of Est\n(actual)", "CIQ EPS Est Date\nMM/dd/yyyy", "CIQ EPS Difference\n($)", "CIQ EPS Surprise\n(%)"]


def est_rows(n=60, month_idx=0, asof="2026-07-01", stale_every=7):
    """Synthetic consensus rows for the same companies as company_rows: the mean drifts one percent a month, estimates dated 0 to 9 days before `asof`, every `stale_every`-th company 100 days old."""
    a = pd.Timestamp(asof)
    rows = []
    for i in range(n):
        mean = (1.0 + i % 9) * (1 + 0.01 * month_idx)
        age = 100 if stale_every and i % stale_every == 0 and i % 10 else i % 10
        rows.append([f"Company {i:02d} Inc.", 1000 + i, f"C{i:07d}0", f"T{i:02d}", mean, mean, mean * 1.1, mean * 0.9, 0.1 * mean, 3 + (i + 2 * month_idx) % 5, SERIAL(str((a - pd.Timedelta(days=age)).date())), 0.1, 5.0])
    return rows


def make_est_file(path, rows, asof="07/01/2026", period="FY+1", mnem=None, drop_period=False):
    pe = [None] * 4 + [period] * 7 + ["FY0", "FY0"]
    r = [[None] * 13, [None] * 13, EST_DISPLAY, list(mnem or EST_MNEM), pe, [None] * 4 + [asof] * 9]
    write_xlsx(path, r + rows)
    return path


EST_ASOF = {"mar": "03/01/2026", "apr": "04/01/2026", "may": "05/01/2026", "jun": "06/01/2026", "jul": "07/01/2026", "aug": "08/01/2026", "sep": "09/01/2026", "oct": "10/01/2026",
            "nov": "11/01/2026"}


def write_est_files(d: Path, n=60, through=10):
    """Consensus files from March to the decision month `through` (10 = October, the default build)."""
    d.mkdir(parents=True, exist_ok=True)
    for i, (k, a) in enumerate(list(EST_ASOF.items())[:through - 2]):
        iso = pd.Timestamp(a).date().isoformat()
        make_est_file(d / f"capiq_est_{k}_2026.xlsx", est_rows(n, i, iso), asof=a)


def ibes_pit_from_est(d: pd.DataFrame, cols: list[str], tmp: Path, n: int) -> pd.DataFrame:
    """Consensus point-in-time table whose values equal the Capital IQ build (so the parity gate is meaningful in a fixture; the real run compares two vendors): an early record for every stock
    (June values, known 2026-05-10, used by the June engine month) and a June record (July values, known 2026-06-20) for the even-numbered stocks only."""
    write_est_files(tmp, n)
    perm = pd.Series([100 + i for i in range(n)])
    fj, _ = est_features(load_est(tmp / "capiq_est_jun_2026.xlsx"), load_est(tmp / "capiq_est_may_2026.xlsx"), load_est(tmp / "capiq_est_mar_2026.xlsx"), make_state(n, smonth="2026-05-01"),
                         pd.Timestamp("2026-06-01"), perm)
    fl, _ = est_features(load_est(tmp / "capiq_est_jul_2026.xlsx"), load_est(tmp / "capiq_est_jun_2026.xlsx"), load_est(tmp / "capiq_est_apr_2026.xlsx"),
                         make_state(n, smonth="2026-06-01"), pd.Timestamp("2026-07-01"), perm)
    out = []
    for f, kd, keep in ((fj, "2026-05-10", perm), (fl, "2026-06-20", perm[perm % 2 == 0])):
        x = d[d["permno"].isin(keep)].copy().set_index("permno")
        x["knowledge_date"] = pd.Timestamp(kd)
        g = f.set_index("permno")
        for c in EST_COLS:
            x[c] = g[c].reindex(x.index)
        if kd == "2026-06-20":
            x["numest_fy1"] += 100                                       # I/B/E/S counts that differ from Capital IQ's: a filled value is below 100, a kept I/B/E/S value is not
        out.append(x.reset_index())
    return pd.concat(out, ignore_index=True)


def chart(days, close, volume=None, adjclose=None, symbol=None) -> dict:
    """A Yahoo chart response. A close of None is a bar without a close; volume and adjclose default to 1.0 and the close."""
    ts = [int(pd.Timestamp(x).timestamp()) for x in days]
    quote = {"close": list(close), "volume": list(volume) if volume is not None else [1.0] * len(ts)}
    result = {"timestamp": ts, "indicators": {"quote": [quote], "adjclose": [{"adjclose": list(adjclose) if adjclose is not None else list(close)}]}}
    return {"chart": {"result": [{**({"meta": {"symbol": symbol}} if symbol else {}), **result}]}}


def write_chart(path: Path, days, close, volume=None, adjclose=None, symbol=None) -> Path:
    """One Yahoo chart file (<permno>.json.gz layout), see `chart`."""
    with gzip.open(path, "wt") as f:
        json.dump(chart(days, close, volume, adjclose, symbol), f)
    return path


def write_yahoo(d: Path, n: int, seed=21):
    """Business-day random walks (2025-01 to 2026-09-30) for the fixture stocks, in the Yahoo chart json.gz layout."""
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-01-02", "2026-09-30")
    for i in range(n):
        px = 20 * np.exp(np.cumsum(rng.normal(0, 0.02, len(days))))
        write_chart(d / f"{100 + i}.json.gz", days, px.tolist(), rng.integers(1e5, 5e5, len(days)).astype(float).tolist(), (px * 1.001).tolist())


def build_base(root: Path, n=60, last_engine="2026-06-01", realistic=True, yahoo_dir: Path | None = None):
    """An engine build whose last decision month is `last_engine`; its flags follow the engine rule applied to the previous state month, as in the real engine."""
    (root / "build/tables").mkdir(parents=True)
    months = [m for m in ("2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01") if m <= last_engine]
    states = pd.concat([make_state(n, seed=3 + i, smonth=sm) for i, sm in enumerate(("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01"))], ignore_index=True)
    jul = company_rows(n, seed=10)
    f = fund_features(load_month(make_file(root.parent / "tmp_jul.xlsx", jul)).table)
    rng = np.random.default_rng(1)
    qrng = np.random.default_rng(5)
    pit = pd.DataFrame({"gvkey": [f"G{i:05d}" for i in range(n)], "knowledge_date": pd.Timestamp("2026-03-15").date(), "datadate": pd.Timestamp("2026-02-28").date(),
                        "rdq": pd.Timestamp("2026-03-14").date(), "earn_chg": qrng.normal(0, 50, n),
                        **{c: qrng.normal(0, 1, n) for c in ("roa", "roe", "asset_growth", "sales_growth", "accruals", "cash_at", "rd_intensity", "sga_intensity", "capex_at", "gp_at_chg", "roa_chg", "sue_ts")}})
    pit.loc[0, "sue_ts"] = np.nan                           # one stock without enough history
    rows = []
    for month in months:
        prev = (pd.Timestamp(month) - pd.DateOffset(months=1)).date()
        fl = engine_flags(states[states["smonth"] == prev].reset_index(drop=True))
        for i in range(n):
            vals = {c: float(f[c].iloc[i] * (1 + rng.normal(0, 0.02))) for c in FEATURES}
            if month == "2026-07-01" and i % 10 == 0:
                vals["bm"] = np.nan                         # an engine gap that Capital IQ should fill
            rows.append({"permno": 100 + i, "month": month, "is_holdout": True, "in_full_panel": True, "in_research": bool(fl["in_research"][i]),
                         "in_investable": bool(fl["in_investable"][i]) if realistic else True, "in_top500": bool(fl["in_top500"][i]), "in_top1000": bool(fl["in_top1000"][i]),
                         "me_rank": float(fl["me_rank"][i]) if fl["me_rank"][i] == fl["me_rank"][i] else None, "me_lag": float(fl["me"][i]), "price_lag": float(fl["price"][i]),
                         "shares_lag": float(fl["shares"][i]), "log_me": 1.0, "log_price": 1.0, "sic2": 35, **vals,
                         "gp_at_chg": float(pit["gp_at_chg"][i]), "sue_ts": float(pit["sue_ts"][i]), "earn_chg_me": float(pit["earn_chg"][i] * 1e6 / fl["me"][i]),
                         "fwd_incomplete_1m": False, "fwd_incomplete_3m": True, "fwd_incomplete_6m": True, "fwd_incomplete_12m": True})
    df = pd.DataFrame(rows)
    if yahoo_dir is not None:                                  # the engine price columns equal the Yahoo build (by construction), except log_me / log_price which the fixture sets itself
        write_yahoo(yahoo_dir, n)
        daily = yp.load_yahoo([100 + i for i in range(n)], yahoo_dir)
        sp = states.assign(p=pd.to_datetime(states["smonth"]).dt.to_period("M"))
        meD = {p: g.set_index("permno")["me"] for p, g in sp.groupby("p")}
        shD = sp.pivot(index="p", columns="permno", values="shares")
        mktD = yp.market_proxy(daily, meD)
        built = yp.price_features(daily, mktD, shD, [m[:7] for m in months], None)
        fq = pd.concat([pd.DataFrame({"gvkey": [f"G{i:05d}" for i in range(n)], "rdq": [pd.Timestamp(d0) + pd.Timedelta(days=i % 5) for i in range(n)]})
                        for d0 in ("2025-11-14", "2026-02-12", "2026-05-12", "2026-08-12")], ignore_index=True)
        fq["datadate"] = fq["rdq"]
        for c in ef.COLS + ef.KD_COLS:
            df[c] = np.nan
        for m in months:
            ev = fq.assign(permno=lambda d: [100 + int(g[1:]) for g in d["gvkey"]]).rename(columns={"rdq": "event_date"})[["permno", "event_date"]]
            ev = ev[ev["event_date"] < pd.Timestamp(m)]
            fe = ef.event_features(daily, mktD, ev, pd.Series([100 + i for i in range(n)]), pd.Timestamp(m)).set_index("permno")
            sel = df["month"] == m
            for c in ef.COLS + ef.KD_COLS:
                df.loc[sel, c] = df.loc[sel, "permno"].map(fe[c]).to_numpy()
        for c in ef.KD_COLS:
            df[c] = pd.to_datetime(df[c]).dt.date
        fq_out = fq
        for c in yp.FEATURES:
            df[c] = np.nan
        for m in months:
            sel = df["month"] == m
            for c in yp.FEATURES:
                if c not in yp.FILL_SKIP:
                    df.loc[sel, c] = df.loc[sel, "permno"].map(built[m[:7]][c]).to_numpy()
    prng = np.random.default_rng(9)
    fam_pit = {}
    for fam, (tbl, _days, cols) in pf.FAMILIES.items():       # one point-in-time record per stock, known before every engine month
        d = pd.DataFrame({"permno": [100 + i for i in range(n)], "knowledge_date": pd.Timestamp("2026-03-20")})
        for c in cols:
            d[c] = pd.Timestamp("2026-03-19") if c == "anndats_act" else prng.normal(0, 1, n)
        d.loc[1, cols[-1]] = np.nan
        if fam == "ibes_cons":
            d = ibes_pit_from_est(d, cols, root.parent / "est_tmp", n)
        fam_pit[tbl] = d
        parts = [pf.asof_permno(d, df.loc[df["month"] == m, "permno"], pd.Timestamp(m), fam).assign(month=m) for m in months]
        df = df.merge(pd.concat(parts), on=["permno", "month"], how="left", suffixes=("", "_dup"))
    # insider and fails-to-deliver inputs; the engine columns come from the same functions (the real run's replication gate is the true check)
    arng = np.random.default_rng(11)
    perm = [100 + i for i in range(n)]
    tx = pd.DataFrame({"permno": np.repeat(perm, 3), "knowledge_date": pd.to_datetime(np.tile(["2026-02-10", "2026-04-20", "2026-05-12"], n)), "code": np.tile(["P", "S", "P"], n),
                       "value": arng.uniform(1e3, 1e6, 3 * n)})
    buyers = pd.DataFrame({"permno": perm * 2, "knowledge_date": pd.to_datetime(["2026-04-20"] * n + ["2026-05-12"] * n), "owner_cik": list(range(1, n + 1)) + list(range(2, n + 2))})
    cov_i = (pd.Timestamp("2006-01-01"), pd.Timestamp("2026-09-30"))
    ftd = pd.DataFrame({"permno": perm * 3, "fmonth": pd.to_datetime(["2026-03-01"] * n + ["2026-04-01"] * n + ["2026-05-01"] * n), "fails_mean": arng.uniform(0, 5e3, 3 * n)})
    cov_f = (pd.Timestamp("2009-07-01"), pd.Timestamp("2026-08-01"))
    shares = {pd.Timestamp(sm): g.set_index("permno")["shares"] for sm, g in states.groupby("smonth")}
    cik_gv = pd.DataFrame({"cik": range(n), "gvkey": [f"G{i:05d}" for i in range(n)]}).iloc[[i for i in range(n) if i != 2]]                      # the third stock has no CIK link
    gvmap = {100 + i: f"G{i:05d}" for i in range(n)}
    ev = pd.concat([pd.DataFrame({"permno": perm, "knowledge_date": pd.Timestamp(kd), "kind": kind, "item_502": i502}) for kd, kind, i502 in
                    (("2026-02-20", "8k", True), ("2026-05-10", "8k", False), ("2026-04-15", "nt", False), ("2026-09-15", "8k", False))], ignore_index=True)
    ev = pd.concat([ev, pd.DataFrame({"permno": perm[::3], "knowledge_date": pd.Timestamp("2026-03-01"), "kind": "sc13d", "item_502": False})], ignore_index=True)
    edgar_ciks = pd.DataFrame({"cik": [i for i in range(n) if i != 4]})                                      # the fifth stock is not on the EDGAR list
    parts = []
    for m in months:
        prev = (pd.Timestamp(m) - pd.DateOffset(months=1)).date()
        me = states[states["smonth"] == prev].set_index("permno")["me"]
        hc = pd.Series([gvmap[x] in set(cik_gv["gvkey"]) for x in perm], index=perm)
        a = af.insider_features(tx[tx["knowledge_date"] < pd.Timestamp(m)], buyers, cov_i, hc, me, pd.Timestamp(m))
        b = af.ftd_features(ftd, shares, cov_f, pd.Series(perm), pd.Timestamp(m))
        cov_ed = pd.Series([gvmap[x] in set(cik_gv["gvkey"]) and int(gvmap[x][1:]) in set(edgar_ciks["cik"]) for x in perm], index=perm)
        parts.append(a.merge(b, on="permno").merge(af.edgar_features(ev, cov_ed, pd.Timestamp("2004-08-23"), pd.Timestamp("2026-09-15"), pd.Timestamp(m)), on="permno").assign(month=m))
    df = df.merge(pd.concat(parts), on=["permno", "month"], how="left", suffixes=("", "_dup"))
    wdb = duckdb.connect((root / "build/work.duckdb").as_posix())
    wdb.register("bu", buyers.assign(knowledge_date=buyers["knowledge_date"].dt.date)), wdb.execute("create table insider_buyers as select * from bu")
    wdb.execute(f"create table insider_coverage as select DATE '{cov_i[0].date()}' filing_start, DATE '{cov_i[1].date()}' filing_end")
    wdb.register("ec", edgar_ciks), wdb.execute("create table edgar_ciks as select * from ec")
    wdb.execute("create table edgar_coverage as select DATE '2004-08-23' item_502_first")
    wdb.execute(f"create table ftd_coverage as select DATE '{cov_f[0].date()}' first_month, DATE '{cov_f[1].date()}' last_month")
    wdb.close()
    con = duckdb.connect()
    if yahoo_dir is not None:
        con.register("fqx", fq_out.assign(rdq=fq_out["rdq"].dt.date, datadate=fq_out["datadate"].dt.date))
        con.execute(f"copy fqx to '{(root / 'build/tables/fund_q.parquet').as_posix()}' (format parquet)")
    for nm, d in (("edgar_events", ev.assign(knowledge_date=ev["knowledge_date"].dt.date, filing_date=ev["knowledge_date"].dt.date, accession="a")), ("insider_tx", tx.assign(knowledge_date=tx["knowledge_date"].dt.date)), ("ftd_monthly", ftd.assign(fmonth=ftd["fmonth"].dt.date)), ("cik_gvkey", cik_gv)):
        con.register(nm, d)
        con.execute(f"copy {nm} to '{(root / 'build/tables' / (nm + '.parquet')).as_posix()}' (format parquet)")
    for tbl, d in fam_pit.items():
        con.register(tbl, d.assign(knowledge_date=d["knowledge_date"].dt.date, **({"anndats_act": d["anndats_act"].dt.date} if "anndats_act" in d else {})))
        con.execute(f"copy {tbl} to '{(root / 'build/tables' / (tbl + '.parquet')).as_posix()}' (format parquet)")
    con.register("p", df.assign(month=lambda d: pd.to_datetime(d["month"]).dt.date))
    con.execute(f"copy (select cast(permno as integer) permno, * exclude (permno) from p) to '{(root / 'build/research_panel.parquet').as_posix()}' (format parquet)")
    con.register("s", states)
    con.register("pg", pd.concat([pd.DataFrame({"permno": [100 + i for i in range(n)], "month": pd.Timestamp(m).date(), "gvkey": pit["gvkey"]}).assign(permno=lambda d: d["permno"].astype("int32")) for m in months]))
    con.execute(f"copy pg to '{(root / 'build/tables/permno_gvkey.parquet').as_posix()}' (format parquet)")
    con.register("pit", pit)
    con.execute(f"copy pit to '{(root / 'build/tables/fund_pit.parquet').as_posix()}' (format parquet)")
    con.execute(f"copy s to '{(root / 'build/tables/security_state.parquet').as_posix()}' (format parquet)")


def build_ciq(d: Path, n=60, through=10):
    """Capital IQ fundamentals and consensus exports for the decision months July to `through` (10 = October, the default build)."""
    d.mkdir(parents=True)
    pr = {"july": ("06/30/2026", "07/01/2026"), "aug": ("07/31/2026", "08/01/2026"), "sep": ("08/31/2026", "09/01/2026"), "oct": ("09/30/2026", "10/01/2026"),
          "nov": ("10/31/2026", "11/01/2026")}
    for k, (p, a) in list(pr.items())[:through - 6]:
        make_file(d / f"capiq_fund_{k}_2026.xlsx", company_rows(n, seed=10), pricing=p, asof=a)      # same companies every month, as in the real files
    write_est_files(d, n, through)
