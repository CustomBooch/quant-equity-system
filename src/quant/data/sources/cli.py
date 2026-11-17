"""`tools/py -m quant data`: the registered data sources.

    tools/py -m quant data list                                   # every source: dataset, entity, licence, stored pulls
    tools/py -m quant data show NAME                              # its declaration and its newest manifest
    tools/py -m quant data pull NAME --start 2024-01-01 [--end 2026-11-17] [--entities A,B]     # fetch, check, store (may use the network)
    tools/py -m quant data check NAME [--as-of 2026-11-01]        # re-read the newest pull, verify its hash, re-run the checks
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from quant.core import licences as L
from quant.data.sources import registry as RG
from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest, validate


def licence_of(name: str) -> L.Source | None:
    return next((s for s in L.SOURCES if s.name == name), None)


def main(argv: list[str] | None = None, root: Path = ST.ROOT) -> int:
    ap = argparse.ArgumentParser(prog="quant data", description="the registered data sources")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    pu = sub.add_parser("pull")
    pu.add_argument("name")
    pu.add_argument("--start", required=True, type=dt.date.fromisoformat)
    pu.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    pu.add_argument("--entities", help="comma-separated identifiers (tickers, permnos, ...); default: all the source offers")
    ch = sub.add_parser("check")
    ch.add_argument("name")
    ch.add_argument("--as-of", type=dt.date.fromisoformat)
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for name, cls in RG.sources().items():
            ds, lic = cls.dataset, licence_of(cls.dataset.licence)
            print(f"{name:24s} {ds.entity:7s} {len(ST.pulls(name, root))} pull(s)   licence: {ds.licence} ({lic.live_use if lic else 'NO ROW'})   {ds.description}")
        return 0
    src = RG.get(a.name)
    ds = src.dataset
    if a.cmd == "show":
        folders = ST.pulls(ds.name, root)
        print(json.dumps({"dataset": {**ds.__dict__}, "newest_pull": json.loads((folders[-1] / "MANIFEST.json").read_text()) if folders else None}, indent=1, default=str))
        return 0
    if a.cmd == "pull":
        lic = licence_of(ds.licence)
        if lic is None:
            print(f"refused: {ds.name} names licence row {ds.licence!r}, which quant.core.licences does not have", file=sys.stderr)
            return 1
        req = PullRequest(a.start, a.end, tuple(x.strip() for x in a.entities.split(",") if x.strip()) if a.entities else None)
        try:
            print(json.dumps(ST.pull(src, req, root), indent=1))
        except ST.Refused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        return 0
    frame = ST.load(src, as_of=a.as_of, root=root)
    problems = validate(frame, ds)
    print(f"{ds.name}: {len(frame)} rows" + (f" usable on {a.as_of}" if a.as_of else "") + ("; checks pass" if not problems else "; " + "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
