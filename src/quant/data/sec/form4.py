"""Original Form 4 filings read from their XML on EDGAR (SPEC_NOV_FILL.md section B), keeping what panel/pit/insider.py keeps from the SEC's quarterly data sets.

    dotenvx run -- python -m quant.data.sec.form4 check   # 1,000 accessions of the 2026q3 data set filed in 2026-09, read from EDGAR and compared -> form4_check.json
    dotenvx run -- python -m quant.data.sec.form4 pull    # original Form 4s filed 2026-10-01..30 for CIKs the build links -> form4_tx.parquet, form4_owners.parquet

Kept per filing: documentType (only "4" is used), the issuer CIK, every reporting owner's CIK, and each non-derivative transaction's code, shares and price per share (codes P and
S are the ones the engine counts). The quarterly data set is not out for 2026q4 until January, so October's filings are read one by one. Files go to data/raw_ext/<SEC_PULL_DIR>
(default sec_fill_2026-11-16). Resumable: each batch read is kept under parts/ and never fetched again; a failed request is recorded, never retried in a loop. The contact comes
from SEC_CONTACT (run under dotenvx) and is never written anywhere; 0.4 s between requests.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from collections.abc import Callable, Iterable
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant.core.paths import REPO
from quant.data.sec import sec_pull

OUT = REPO / "data/raw_ext" / os.environ.get("SEC_PULL_DIR", "sec_fill_2026-11-16")
DATASET = REPO / "data/raw_ext/six_2026-10-13/sec_insider/2026q3.zip"
TABLES = REPO / "panel/_build/ext_2026-10-22a/build/tables"
CHECK_N, CHECK_SEED = 1000, 20261116
CHECK_FROM, CHECK_TO = pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-30")
PULL_FROM, PULL_TO = pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-30")
BATCH = 200
CODES = ("P", "S")


@dataclass
class Filing:
    """One Form 4 as the data set would hold it: document type, issuer CIK, owner CIKs, and (code, shares, price) per non-derivative transaction."""
    doc_type: str | None
    issuer_cik: int | None
    owners: list[int] = field(default_factory=list)
    tx: list[tuple[str, float | None, float | None]] = field(default_factory=list)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", block, re.S)
    return m.group(1).strip() if m else None


def _value(block: str, name: str) -> float | None:
    """The number in <name><value>x</value></name>; None when absent or not a number (a footnote-only price, as the data set leaves it empty)."""
    inner = _tag(block, name)
    v = _tag(inner, "value") if inner is not None else None
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _cik(text: str | None) -> int | None:
    return int(text) if text and text.strip().isdigit() else None


def parse(text: str) -> Filing | None:
    """The Form 4 in a full submission text (its <ownershipDocument>); None if there is none. Text patterns only: the document's structure is fixed and no XML parser
    is given untrusted input."""
    m = re.search(r"<ownershipDocument>(.*?)</ownershipDocument>", text, re.S)
    if m is None:
        return None
    doc = m.group(1)
    issuer = _tag(doc, "issuer") or ""
    owners = [c for c in (_cik(x) for x in re.findall(r"<rptOwnerCik>\s*(\d+)\s*</rptOwnerCik>", doc)) if c is not None]
    tx = []
    for t in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", doc, re.S):
        code = _tag(_tag(t, "transactionCoding") or "", "transactionCode")
        amounts = _tag(t, "transactionAmounts") or ""
        tx.append((code or "", _value(amounts, "transactionShares"), _value(amounts, "transactionPricePerShare")))
    return Filing(_tag(doc, "documentType"), _cik(_tag(issuer, "issuerCik")), sorted(set(owners)), tx)


def cents(x: float | None) -> float | None:
    """A number as the SEC's quarterly data set stores it: two decimals, half up (the XML has full precision; the data set, and so the engine, carries 532.08 for 532.0826)."""
    if x is None or pd.isna(x):
        return None
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), ROUND_HALF_UP))


def key(accession: str, code: str, shares: float | None, price: float | None) -> tuple[str, str, float | None, float | None]:
    """An event's identity for the comparison: numbers rounded to 4 decimals, as text in the data set and decimals in the XML."""
    return (accession, code, None if shares is None or np.isnan(shares) else round(shares, 4), None if price is None or np.isnan(price) else round(price, 4))


def compare(ds_events: Iterable[tuple[Any, ...]], rd_events: Iterable[tuple[Any, ...]], ds_meta: dict[str, tuple[int, set[int]]], rd_meta: dict[str, tuple[int, set[int]]]) -> dict[str, Any]:
    """SPEC_NOV_FILL B check (a): share of the data set's P/S events the reader produces and of the reader's in the data set (as multisets), and the share of accessions
    whose issuer CIK and owner set agree. Only accessions both sides have are compared."""
    from collections import Counter

    both = set(ds_meta) & set(rd_meta)
    a = Counter(e for e in ds_events if e[0] in both)
    b = Counter(e for e in rd_events if e[0] in both)
    common = sum((a & b).values())
    agree = sum(1 for k in both if ds_meta[k][0] == rd_meta[k][0] and ds_meta[k][1] == rd_meta[k][1])
    return {"accessions_compared": len(both), "dataset_events": sum(a.values()), "reader_events": sum(b.values()),
            "dataset_found_by_reader": round(common / max(sum(a.values()), 1), 4), "reader_found_in_dataset": round(common / max(sum(b.values()), 1), 4),
            "issuer_and_owners_agree": round(agree / max(len(both), 1), 4)}


def read_batches(todo: list[tuple[str, int]], name: str, getter: Callable[[str], bytes]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read every (accession, cik) in `todo` from EDGAR in batches kept under OUT/parts/<name>_*.parquet (an accession already in a part is not read again).
    Returns (transactions, owners, status) over all parts."""
    parts = OUT / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    done = set()
    for p in parts.glob(f"{name}_status_*.parquet"):
        done |= set(pd.read_parquet(p)["accession"])
    left = [t for t in todo if t[0] not in done]
    n0 = len(list(parts.glob(f"{name}_status_*.parquet")))
    for b in range(0, len(left), BATCH):
        tx, ow, st = [], [], []
        for acc, cik in left[b:b + BATCH]:
            try:
                f = parse(getter(f"{sec_pull.SEC}/Archives/edgar/data/{cik}/{acc}.txt").decode("latin-1"))
            except Exception as e:  # noqa: BLE001 - recorded, never retried in a loop
                st.append({"accession": acc, "ok": False, "error": repr(e)[:120]})
                continue
            if f is None:
                st.append({"accession": acc, "ok": False, "error": "no ownershipDocument"})
                continue
            st.append({"accession": acc, "ok": True, "error": None, "doc_type": f.doc_type, "issuer_cik": f.issuer_cik})
            tx += [{"accession": acc, "code": c, "shares": s, "price": p} for c, s, p in f.tx]
            ow += [{"accession": acc, "owner_cik": o} for o in f.owners]
        i = n0 + b // BATCH
        pd.DataFrame(tx, columns=["accession", "code", "shares", "price"]).to_parquet(parts / f"{name}_tx_{i:05d}.parquet", index=False)
        pd.DataFrame(ow, columns=["accession", "owner_cik"]).to_parquet(parts / f"{name}_owners_{i:05d}.parquet", index=False)
        pd.DataFrame(st, columns=["accession", "ok", "error", "doc_type", "issuer_cik"]).to_parquet(parts / f"{name}_status_{i:05d}.parquet", index=False)
        print(name, min(b + BATCH, len(left)), "of", len(left), "read this run", flush=True)

    def all_of(kind: str) -> pd.DataFrame:
        fs = sorted(parts.glob(f"{name}_{kind}_*.parquet"))
        return pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True) if fs else pd.DataFrame()
    return all_of("tx"), all_of("owners"), all_of("status")


def dataset_tables(path: Path = DATASET) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """SUBMISSION, NONDERIV_TRANS and REPORTINGOWNER of a quarterly data set, as text (the engine's read: tab separated, no quoting)."""
    with zipfile.ZipFile(path) as zf:
        names = {n.split("/")[-1]: n for n in zf.namelist()}

        def read(t: str) -> pd.DataFrame:
            return pd.read_csv(io.BytesIO(zf.read(names[t])), sep="\t", dtype=str, quoting=3, on_bad_lines="skip")
        return read("SUBMISSION.tsv"), read("NONDERIV_TRANS.tsv"), read("REPORTINGOWNER.tsv")


def check(getter: Callable[[str], bytes] = sec_pull.get) -> dict[str, Any]:
    """SPEC_NOV_FILL B check (a)."""
    sub, ntx, own = dataset_tables()
    sub["fd"] = pd.to_datetime(sub["FILING_DATE"], format="%d-%b-%Y", errors="coerce")
    s = sub[(sub["DOCUMENT_TYPE"] == "4") & sub["fd"].between(CHECK_FROM, CHECK_TO)].drop_duplicates("ACCESSION_NUMBER", keep=False)
    s = s[s["ISSUERCIK"].str.isdigit().fillna(False)]
    pick = sorted(np.random.default_rng(CHECK_SEED).choice(sorted(s["ACCESSION_NUMBER"]), size=min(CHECK_N, len(s)), replace=False))
    s = s.set_index("ACCESSION_NUMBER").loc[pick]
    t = ntx[ntx["ACCESSION_NUMBER"].isin(pick) & ntx["TRANS_CODE"].isin(CODES)]
    o = own[own["ACCESSION_NUMBER"].isin(pick)]
    ds_events = [key(a, c, pd.to_numeric(sh, errors="coerce"), pd.to_numeric(pr, errors="coerce"))
                 for a, c, sh, pr in zip(t["ACCESSION_NUMBER"], t["TRANS_CODE"], t["TRANS_SHARES"], t["TRANS_PRICEPERSHARE"], strict=True)]
    owners = o.groupby("ACCESSION_NUMBER")["RPTOWNERCIK"].agg(lambda x: {int(v) for v in x if str(v).isdigit()})
    ds_meta = {a: (int(s.loc[a, "ISSUERCIK"]), owners.get(a, set())) for a in pick}
    rtx, row, rst = read_batches([(a, int(s.loc[a, "ISSUERCIK"])) for a in pick], "check", getter)
    ok = rst[rst["ok"]] if len(rst) else rst
    rtx = rtx[rtx["code"].isin(CODES)] if len(rtx) else rtx
    rd_events = [key(a, c, cents(sh), cents(pr)) for a, c, sh, pr in zip(rtx.get("accession", []), rtx.get("code", []), rtx.get("shares", []), rtx.get("price", []), strict=True)]
    r_owners = row.groupby("accession")["owner_cik"].agg(set) if len(row) else pd.Series(dtype=object)
    rd_meta = {a: (int(i) if pd.notna(i) else -1, r_owners.get(a, set())) for a, i in zip(ok.get("accession", []), ok.get("issuer_cik", []), strict=True)}
    res = {"sample": len(pick), "seed": CHECK_SEED, "read_ok": int(len(ok)), "read_failed": int(len(rst) - len(ok)), **compare(ds_events, rd_events, ds_meta, rd_meta)}
    res["pass"] = bool(res["dataset_found_by_reader"] >= 0.99 and res["reader_found_in_dataset"] >= 0.99 and res["issuer_and_owners_agree"] >= 0.99)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "form4_check.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))
    return res


def linked_ciks() -> set[int]:
    """CIKs the build links to a gvkey that has a permno link (the filings that can reach a bridge row)."""
    cg = pd.read_parquet(TABLES / "cik_gvkey.parquet")[["cik", "gvkey"]]
    gl = pd.read_parquet(TABLES / "gvkey_links.parquet")[["gvkey"]].drop_duplicates()
    return set(cg.merge(gl, on="gvkey")["cik"].astype("int64"))


def listing(index_text: str, ciks: set[int], start: pd.Timestamp = PULL_FROM, end: pd.Timestamp = PULL_TO) -> pd.DataFrame:
    """Original Form 4s in a form.idx filed in [start, end] under a CIK in `ciks`: one row per accession (accession, cik, filingDate)."""
    d = sec_pull.parse_form_idx(index_text)
    d = d[(d["form"] == "4") & d["filingDate"].between(start, end) & d["cik"].isin(ciks)].copy()
    d["accession"] = d["path"].str.extract(r"/([0-9\-]+)\.txt$")[0]
    return d.sort_values(["accession", "cik"]).drop_duplicates("accession")[["accession", "cik", "filingDate"]].reset_index(drop=True)


def pull(getter: Callable[[str], bytes] = sec_pull.get) -> dict[str, Any]:
    """SPEC_NOV_FILL B: October's original Form 4s, in the engine's event form (panel/pit/insider.py)."""
    idx = getter(f"{sec_pull.SEC}/Archives/edgar/full-index/2026/QTR4/form.idx").decode("latin-1")
    lst = listing(idx, linked_ciks())
    tx, ow, st = read_batches(list(zip(lst["accession"], lst["cik"].astype(int), strict=True)), "pull", getter)
    ok = st[st["ok"] & (st["doc_type"] == "4")]
    t = tx[tx["accession"].isin(ok["accession"]) & tx["code"].isin(CODES)].merge(ok[["accession", "issuer_cik"]], on="accession")
    t = t.merge(lst[["accession", "filingDate"]], on="accession").rename(columns={"issuer_cik": "cik", "filingDate": "filing_date"})
    t["shares"], t["price"] = t["shares"].map(cents).astype(float), t["price"].map(cents).astype(float)    # the data set's precision, as the engine read it
    t["value"] = (t["shares"] * t["price"]).where((t["shares"] > 0) & (t["price"] > 0))
    t["knowledge_date"] = t["filing_date"] + pd.Timedelta(days=1)
    t[["accession", "code", "cik", "filing_date", "shares", "price", "value", "knowledge_date"]].to_parquet(OUT / "form4_tx.parquet", index=False)
    ow[ow["accession"].isin(ok["accession"])].to_parquet(OUT / "form4_owners.parquet", index=False)
    res = {"listed": len(lst), "read_ok": int(st["ok"].sum()), "read_failed": int((~st["ok"]).sum()), "original_form4": len(ok), "ps_events": len(t),
           "share_read": round(float(st["ok"].sum()) / max(len(lst), 1), 4), "filing_dates": [str(lst["filingDate"].min().date()), str(lst["filingDate"].max().date())]}
    res["pass"] = res["share_read"] >= 0.99
    (OUT / "form4_pull.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    {"check": check, "pull": pull}[sys.argv[1]]()
