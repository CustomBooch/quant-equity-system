"""Refresh the free Yahoo daily bars read by the bridge price features and the target book's prices (quant.features.ciqfill.yahoo_price): one chart file per permno.

    tools/py -m quant.data.yahoo.yahoo_bars --to 2026-10-31 --out data/raw/Yahoo/free_2026/yahoo_2026-10-31 [--from-dir data/raw/Yahoo/free_2026/yahoo]

Each stock keeps the Yahoo symbol of its file in --from-dir (`meta.symbol`), so the refreshed set covers the same stocks under the same symbols as the bars the
current release was built from; the bars run from START to --to (inclusive). A new folder is written and the old one is never touched: a release records the digest
of the folder it was built from as an input. Resumable (files already in --out are kept); MANIFEST.json lists each file's URL, time and SHA-256; a symbol Yahoo no
longer returns is listed in the manifest, not guessed. Public data, read only: one request at a time with a pause, a few retries with back-off.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from quant.core.paths import YAHOO_DAILY

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={a}&period2={b}&interval=1d&events=div%2Csplits"
START = dt.date(2023, 11, 1)                                 # where the bars of 2026-10-04 start (archive/legacy_research/forward_2026/acquire.py)
PAUSE, TRIES = 0.5, 4


def symbols(src: Path) -> dict[int, str]:
    """permno -> the Yahoo symbol recorded in its chart file."""
    out = {}
    for f in sorted(src.glob("*.json.gz")):
        with gzip.open(f) as g:
            out[int(f.name.split(".")[0])] = json.load(g)["chart"]["result"][0]["meta"]["symbol"]
    return out


def epoch(day: dt.date) -> int:
    """Seconds since 1970 at 00:00 UTC of `day` (Yahoo's period bounds)."""
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())


def fetch(url: str, pause: float = PAUSE) -> bytes | None:
    """The chart response, or None if Yahoo answers that it has no result for the symbol (a chart with no result, or HTTP 404). Anything else that is not a
    chart response (a network error, a rate-limit page) is retried TRIES times with back-off and then raised, so a stock is never dropped by a passing failure."""
    for k in range(TRIES):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}), timeout=60) as r:
                body = r.read()
            time.sleep(pause)
            return body if json.loads(body)["chart"]["result"] else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if k == TRIES - 1:
                raise
        except (OSError, ValueError, KeyError, TypeError):
            if k == TRIES - 1:
                raise
        time.sleep(2 ** k)
    return None


def refresh(src: Path, out: Path, end: dt.date, pause: float = PAUSE) -> dict[str, Any]:
    """Write out/<permno>.json.gz for every stock of `src`, bars START..end; returns the manifest (also written to out/MANIFEST.json)."""
    if out.resolve() == src.resolve():
        raise SystemExit("--out must be a new folder: the releases built from the source folder record its digest")
    out.mkdir(parents=True, exist_ok=True)
    man_path, partial = out / "MANIFEST.json", out / "MANIFEST.partial.json"     # MANIFEST.json only once finished: readers take it as "complete"
    known = partial if partial.exists() else man_path
    man: dict[str, Any] = json.loads(known.read_text()) if known.exists() else {"source_symbols": str(src), "start": str(START), "end": str(end), "files": {}, "not_returned": {}}
    if man["end"] != str(end):
        raise SystemExit(f"{out} holds bars to {man['end']}, not {end}: use a new folder")
    for i, (permno, sym) in enumerate(symbols(src).items()):
        f = out / f"{permno}.json.gz"
        if f.exists() or str(permno) in man["not_returned"]:
            continue
        url = URL.format(symbol=sym, a=epoch(START), b=epoch(end + dt.timedelta(days=1)))
        body = fetch(url, pause)
        if body is None:
            man["not_returned"][str(permno)] = sym
        else:
            f.write_bytes(gzip.compress(body, mtime=0))
            man["files"][f.name] = {"url": url, "utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "sha256": hashlib.sha256(body).hexdigest()}
        if i % 200 == 0:
            partial.write_text(json.dumps(man, indent=1) + "\n")
    man_path.write_text(json.dumps(man, indent=1) + "\n")
    partial.unlink(missing_ok=True)
    return man


def main(argv: list[str] | None = None) -> int:
    """Refresh the bars from the command line and print what was written."""
    ap = argparse.ArgumentParser(prog="python -m quant.data.yahoo.yahoo_bars", description=__doc__.split("\n")[0])
    ap.add_argument("--to", required=True, help="last day of bars, YYYY-MM-DD (the end of the month before the decision month)")
    ap.add_argument("--out", required=True, help="a new folder for the refreshed bars")
    ap.add_argument("--from-dir", default=str(YAHOO_DAILY), help="bars whose symbols to refresh (default: the folder the current release used)")
    a = ap.parse_args(argv)
    man = refresh(Path(a.from_dir), Path(a.out), dt.date.fromisoformat(a.to))
    print(f"{len(man['files'])} files, {len(man['not_returned'])} symbols not returned; manifest {Path(a.out) / 'MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
