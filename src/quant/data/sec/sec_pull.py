"""Public SEC files for the October bridge month (SPEC_OCT_FILL.md): EDGAR form indexes, 8-K item numbers, fails-to-deliver and Form 13F data sets.

    SEC_CONTACT="<contact>" python -m quant.data.sec.sec_pull index|items|ftd|f13|all

index   EDGAR full-index form.idx for 2026 QTR3 and QTR4 -> index.parquet (8-K, NT 10-K/10-Q, Schedule 13D; every filing date, parsed from the right)
items   submissions API for each CIK that filed an 8-K after the local submissions extract ends, for the item numbers -> items.parquet
ftd     cnsfails half-month zips 2026-06 to 2026-09 -> ftd/
f13     Form 13F data sets by filing window, 2025-09 to 2026-08 -> f13/
All files go to data/raw_ext/sec_live_2026-10-26 with a MANIFEST.json (URL, bytes, SHA-256, UTC time). The contact string is read from the environment and is never written to a file.
0.4 s between requests; no retry loops; an existing file is never downloaded again.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

import duckdb
import pandas as pd

from quant.core.sql import one_row
from quant.core.paths import REPO

OUT = REPO / "data/raw_ext" / os.environ.get("SEC_PULL_DIR", "sec_live_2026-10-26")      # a later pull goes to its own folder: SEC_PULL_DIR=sec_fill_<date>
SUBS = REPO / "data/raw/SEC/edgar_submissions/edgar_submissions.parquet"
SEC = "https://www.sec.gov"
FORMS = {"8-K": "8k", "NT 10-K": "nt", "NT 10-Q": "nt", "SC 13D": "sc13d", "SC 13D/A": "sc13d", "SCHEDULE 13D": "sc13d", "SCHEDULE 13D/A": "sc13d"}
QUARTERS = ("2026/QTR3", "2026/QTR4")
FTD_FILES = [f"cnsfails2026{m:02d}{h}.zip" for m in (6, 7, 8, 9) for h in "ab"]
F13_FILES = {"01sep2025-30nov2025_form13f.zip": "structureddata", "01dec2025-28feb2026_form13f.zip": "structureddata",
             "01mar2026-31may2026_form13f.zip": "structureddata", "01jun2026-31aug2026_form13f.zip": "datastandardsinnovation"}
PAUSE = 0.4


def ua() -> dict[str, str]:
    """The User-Agent header the SEC's fair-access policy asks for, with the contact from SEC_CONTACT (exits if it is not set; never written anywhere)."""
    c = os.environ.get("SEC_CONTACT", "").strip()
    if not c:
        sys.exit("set SEC_CONTACT (SEC fair-access policy)")
    return {"User-Agent": f"personal academic research {c}"}


def get(url: str) -> bytes:
    """The body of one GET request, then the fair-access pause."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=ua()), timeout=600) as r:
        body: bytes = r.read()
    time.sleep(PAUSE)
    return body


def record(name: str, url: str, body: bytes) -> None:
    """Add one downloaded file to the manifest."""
    OUT.mkdir(parents=True, exist_ok=True)
    mf = OUT / "MANIFEST.json"
    m: dict[str, Any] = json.loads(mf.read_text()) if mf.exists() else {"files": {}}
    m["files"][name] = {"url": url, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "downloaded_utc": datetime.now(UTC).isoformat(timespec="seconds")}
    mf.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")


def fetch(name: str, url: str) -> None:
    """Download `url` to OUT/`name` unless that file exists, and record it in the manifest."""
    p = OUT / name
    if p.exists():
        print("have", name)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    body = get(url)
    p.write_bytes(body)
    record(name, url, body)
    print("got", name, len(body), "bytes", flush=True)


def parse_form_idx(text: str) -> pd.DataFrame:
    """Rows of an EDGAR form.idx. The form type takes the width of its header column (it can contain spaces); CIK, date filed and file name are the last three
    whitespace-separated fields, so a long company name cannot shift them."""
    lines = text.splitlines()
    head = next(i for i, ln in enumerate(lines) if ln.startswith("Form Type"))
    width = lines[head].index("Company Name")
    rows = []
    for ln in lines[head + 2:]:
        parts = ln.rsplit(None, 3)
        if len(parts) != 4 or not parts[1].isdigit():
            continue
        rows.append({"form": ln[:width].strip(), "cik": int(parts[1]), "filingDate": pd.Timestamp(parts[2]), "path": parts[3]})
    return pd.DataFrame(rows, columns=["form", "cik", "filingDate", "path"])


def index() -> None:
    """The 2026 QTR3 and QTR4 form indexes, reduced to the forms the engine counts, written to index.parquet."""
    parts = []
    for q in QUARTERS:
        url = f"{SEC}/Archives/edgar/full-index/{q}/form.idx"
        body = get(url)
        record(f"form_idx_{q.replace('/', '_')}", url, body)
        parts.append(parse_form_idx(body.decode("latin-1")))
    d = pd.concat(parts, ignore_index=True)
    d = d[d["form"].isin(FORMS)].copy()
    d["kind"] = d["form"].map(FORMS)
    d["accession"] = d["path"].str.extract(r"/([0-9\-]+)\.txt$")[0]
    OUT.mkdir(parents=True, exist_ok=True)
    d.to_parquet(OUT / "index.parquet", index=False)
    print("index rows", len(d), d["kind"].value_counts().to_dict(), "| filing dates", d["filingDate"].min().date(), "to", d["filingDate"].max().date())


def reusable_items(folder: str | None) -> pd.DataFrame:
    """The items rows an earlier pull already holds (SEC_PULL_REUSE=<folder under data/raw_ext>), so only 8-Ks it lacks are requested again; empty when none is named."""
    cols = ["accession", "form", "filingDate", "items", "cik"]
    if not folder:
        return pd.DataFrame(columns=cols)
    return pd.read_parquet(REPO / "data/raw_ext" / folder / "items.parquet")[cols]


def items() -> None:
    """Item numbers of the 8-Ks filed after the local submissions extract ends (the engine's item_502 needs them)."""
    last = one_row(duckdb.connect(), f"SELECT max(filingDate) FROM read_parquet('{SUBS.as_posix()}')")[0]
    d = pd.read_parquet(OUT / "index.parquet")
    new8k = d[(d["kind"] == "8k") & (d["filingDate"] > pd.Timestamp(last))]
    if through := os.environ.get("SEC_PULL_THROUGH"):                          # a decision month needs filings to its last known day only
        new8k = new8k[new8k["filingDate"] <= pd.Timestamp(through)]
    prior = reusable_items(os.environ.get("SEC_PULL_REUSE"))
    todo = sorted(new8k.loc[~new8k["accession"].isin(set(prior["accession"])), "cik"].unique())
    out: list[pd.DataFrame] = []
    errors: list[dict[str, Any]] = []
    t0 = time.time()
    for i, cik in enumerate(todo):
        try:
            j = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))["filings"]["recent"]
        except Exception as e:  # noqa: BLE001 - recorded, no retry
            errors.append({"cik": int(cik), "error": repr(e)[:120]})
            continue
        f = pd.DataFrame({"accession": j["accessionNumber"], "form": j["form"], "filingDate": pd.to_datetime(j["filingDate"]), "items": j["items"]})
        out.append(f[(f["form"] == "8-K") & (f["filingDate"] > pd.Timestamp(last))].assign(cik=int(cik)))
        if i % 200 == 0:
            print("items", i, "of", len(todo), round((time.time() - t0) / 60, 1), "min", flush=True)
    pd.concat([prior, *out], ignore_index=True).drop_duplicates("accession").to_parquet(OUT / "items.parquet", index=False)
    (OUT / "items_errors.json").write_text(json.dumps({"after": str(last), "ciks": len(todo), "reused_rows": len(prior), "errors": errors}, indent=1) + "\n")
    print("items rows", sum(len(x) for x in out), "ciks", len(todo), "errors", len(errors))


def ftd() -> None:
    """The fails-to-deliver half-month files FTD_FILES."""
    for f in FTD_FILES:
        fetch(f"ftd/{f}", f"{SEC}/files/data/fails-deliver-data/{f}")


def f13() -> None:
    """The Form 13F data sets F13_FILES."""
    for f, folder in F13_FILES.items():
        fetch(f"f13/{f}", f"{SEC}/files/{folder}/data/form-13f-data-sets/{f}")


if __name__ == "__main__":
    steps = {"index": index, "items": items, "ftd": ftd, "f13": f13}
    for s in (list(steps) if sys.argv[1] == "all" else [sys.argv[1]]):
        steps[s]()
