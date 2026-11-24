"""Extend the SEC Form 4 insider-transaction archives (SEC DERA "Insider Transactions Data Sets", public domain) for the extended build.

    SEC_CONTACT="<contact string>" python -m quant.data.sec.sec_form4 [--through 2026q2]
    -> data/raw/SEC/sec_insider/YYYYqN.zip   (existing archives hard-linked from data/raw/SEC/sec_insider_frozen, new quarters downloaded)

SEC's fair-access policy requires a User-Agent that identifies the requester with contact details. The contact is read from the SEC_CONTACT environment variable and is
never written to a file. Existing archives are not modified. Only complete quarters up to --through are fetched; 0.5 s between requests.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from quant.core.paths import REPO

SRC = REPO / "data/raw/SEC/sec_insider_frozen"
DST = REPO / "data/raw/SEC/sec_insider"
URLS = ["https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
        "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{q}_form345.zip"]      # SEC moved the files from 2026q2


def quarters(first: str, last: str) -> Iterator[str]:
    y, q = int(first[:4]), int(first[5])
    ly, lq = int(last[:4]), int(last[5])
    while (y, q) <= (ly, lq):
        yield f"{y}q{q}"
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", default="2026q2")
    a = ap.parse_args(argv)
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        sys.exit("set SEC_CONTACT to a contact string for the User-Agent (SEC fair-access policy)")
    DST.mkdir(parents=True, exist_ok=True)
    for z in sorted(SRC.glob("*.zip")):                                                # existing archives: hard link (no extra disk), copy as a fallback
        t = DST / z.name
        if not t.exists():
            try:
                os.link(z, t)
            except OSError:
                shutil.copy2(z, t)
    have = {p.stem for p in DST.glob("*.zip")}
    last = max(have)
    for q in quarters(last, a.through):
        if q in have:
            continue
        data = None
        for url in URLS:
            req = urllib.request.Request(url.format(q=q), headers={"User-Agent": f"personal academic research {contact}"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                break
            except urllib.error.HTTPError as e:
                print(f"{q}: HTTP {e.code} at {url.split('/')[5]}", flush=True)
            time.sleep(0.5)
        if data is None:
            continue
        (DST / f"{q}.zip").write_bytes(data)
        print(f"{q}: {len(data):,} bytes", flush=True)
        time.sleep(0.5)
    print("archives:", len(list(DST.glob("*.zip"))), "last:", max(p.stem for p in DST.glob("*.zip")))


if __name__ == "__main__":
    main()
