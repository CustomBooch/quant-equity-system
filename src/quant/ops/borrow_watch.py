"""Borrow watch: is every short the model holds still easy to borrow? A monitor, not a gate. It reads two local files and nothing else: the tranche ledger
(what the model believes each tranche holds, quant.execution.ledger) and the newest Alpaca asset list. It places, changes and cancels no order and makes no
request; refresh the list first with `dotenvx run -- tools/py -m quant.execution.brokers.alpaca.pull_assets`.

    tools/py -m quant borrow-watch              # one line per held short; exit 1 on any finding, 2 if there is no asset list
    tools/py -m quant borrow-watch --json

A held short is a finding when its symbol is missing from the list or the list says it is not active, not tradable, not shortable or not easy to borrow:
such a name can be recalled or charged a hard-to-borrow fee, and the owner decides what to do (SPEC amendment A11; the wiki page Home). The list itself is a
finding when it is more than 3 days old, because the flags change daily and an old list proves nothing. Weights are the newest target book's, not marked to
market.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
from pathlib import Path
from typing import Any

import pandas as pd

from quant.core.paths import REPO
from quant.data.instruments import alpaca as AL
from quant.execution import ledger as LG

MAX_LIST_AGE_DAYS = 3
BOOKS = REPO / "lab/paper/results"


def held_shorts(ledger: dict[str, Any]) -> dict[str, float]:
    """Symbol -> shares (negative) of every name the tranches hold short in total."""
    return {t: q for t, q in LG.aggregate(ledger).items() if q < 0}


def target_weights(folder: Path = BOOKS) -> dict[str, float]:
    """Symbol -> weight in the account (negative for shorts) of the newest Alpaca target book; {} if there is none."""
    books = sorted(glob.glob(str(folder / "target_book_*_alpaca.csv")))
    if not books:
        return {}
    b = pd.read_csv(books[-1])
    return {str(t): float(w) for t, w in zip(b["ticker"], b["w"], strict=True)}


def check(shorts: dict[str, float], assets: pd.DataFrame) -> dict[str, list[str]]:
    """Symbol -> the reasons it cannot be shorted by the list's flags (empty list: fine)."""
    by = assets.drop_duplicates("symbol").set_index(assets.drop_duplicates("symbol")["symbol"].astype(str))
    out: dict[str, list[str]] = {}
    for t in shorts:
        if t not in by.index:
            out[t] = ["not in the asset list"]
            continue
        row = by.loc[t]
        out[t] = [reason for ok, reason in ((row["status"] == "active", "not active"), (bool(row["tradable"]), "not tradable"), (bool(row["shortable"]), "not shortable"),
                                              (bool(row["easy_to_borrow"]), "not easy to borrow")) if not ok]
    return out


def report(ledger: dict[str, Any], assets: pd.DataFrame, list_day: str | None, today: dt.date, weights: dict[str, float] | None = None) -> dict[str, Any]:
    """The watch's findings: stale list, and each held short that fails its flags."""
    shorts = held_shorts(ledger)
    reasons = check(shorts, assets)
    w = weights or {}
    rows: list[dict[str, Any]] = [{"symbol": t, "shares": q, "target_weight": w.get(t), "reasons": reasons[t]} for t, q in sorted(shorts.items())]
    age = None if list_day is None else (today - dt.date.fromisoformat(list_day)).days
    findings = [f"{r['symbol']}: {', '.join(r['reasons'])}" for r in rows if r["reasons"]]
    if age is None or age > MAX_LIST_AGE_DAYS:
        findings.insert(0, f"asset list is {'missing' if age is None else f'{age} days old'}; refresh it before relying on this check")
    weights_held = [abs(r["target_weight"]) for r in rows if r["target_weight"] is not None]
    return {"list_date": list_day, "list_age_days": age, "short_names": len(rows), "shorts": rows, "largest_short_target_weight": max(weights_held, default=None),
            "findings": findings}


def main(argv: list[str] | None = None, *, today: dt.date | None = None, ledger_path: Path | None = None, books: Path = BOOKS) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    day = AL.list_date()
    if day is None:
        print("REFUSED: no Alpaca asset list; run: dotenvx run -- tools/py -m quant.execution.brokers.alpaca.pull_assets")
        return 2
    out = report(LG.load(ledger_path), AL.latest_assets(), day, today or dt.date.today(), target_weights(books))
    if a.json:
        print(json.dumps(out, indent=1))
        return 1 if out["findings"] else 0
    print(f"asset list {out['list_date']} ({out['list_age_days']} days old); {out['short_names']} held short(s)")
    for r in out["shorts"]:
        w = "" if r["target_weight"] is None else f"  target {r['target_weight']:+.2%}"
        print(f"{'FINDING' if r['reasons'] else 'OK     '} {r['symbol']:8s} {r['shares']:>10.2f} shares{w}  {', '.join(r['reasons'])}")
    for f in out["findings"]:
        print("FINDING", f)
    print(f"borrow-watch: {len(out['findings'])} finding(s)")
    return 1 if out["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
