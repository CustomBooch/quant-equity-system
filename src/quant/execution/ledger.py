"""PAPER per-tranche share ledger (what the model believes each tranche holds). JSON, written atomically; the broker's positions are the truth and `engine.reconcile` compares the two."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from quant.portfolio.plan import ROOT

LEDGER = ROOT / "ledger.json"


def load(path: Path | None = None) -> dict[str, dict[str, float]]:
    p = path or LEDGER
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def tranche(ledger: dict[str, Any], i: int) -> dict[str, float]:
    return dict(ledger.get(str(i), {}))


def record_batch(ledger: dict[str, Any], i: int, new_shares: dict[str, float], path: Path | None = None) -> dict[str, Any]:
    """Store tranche i's holdings after a batch that was sent and reconciled."""
    out = {**ledger, str(i): {t: q for t, q in new_shares.items() if q}}
    p = path or LEDGER
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return out


def aggregate(ledger: dict[str, Any]) -> dict[str, float]:
    agg: dict[str, float] = {}
    for h in ledger.values():
        for t, q in h.items():
            agg[t] = round(agg.get(t, 0.0) + q, 4)
    return agg
