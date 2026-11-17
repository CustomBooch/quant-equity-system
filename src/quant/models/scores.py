"""PAPER scores (SPEC amendment A4): EXAMPLE for one decision month from the current master release. python -m quant.models.scores [--month 2026-10-01]

the model (defined, fitted and applied by quant.models.definition) scores the master's investable rows of the month, ranked within the
month. Writes lab/paper/scores/<month>__EXAMPLE.parquet (permno, score, q, me_lag, price_lag) and a .json record (release, its hash, rows, input coverage, seed agreement).
Rails (A4, fixed before any score): the release named by panel/MASTER_CURRENT.json must verify by hash; each gate it failed must be either waived by the owner in the pointer or a
per-feature parity gate (whose failure means that feature was left empty, which the coverage rail then judges); and every model input must cover at least its coverage in the last
engine month minus 10 points (the quant.validation.audit_panel rule; correction of 2026-10-24, see SPEC A4). Otherwise nothing is written. No account or broker is touched.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from quant.core.frozen import assert_frozen_intact
from quant.models import definition as LC
from quant.core.paths import REPO
from quant.core.release import pointer as PT
from quant.core.release import verify_release

OUT = REPO / "lab/paper/scores"
MAX_COVERAGE_DROP = 0.10
LAST_ENGINE_MONTH = "2026-06-01"
PARITY_PREFIXES = ("est_parity_", "px_parity_", "ev_parity_", "sec_live_", "fill_parity_")        # a failed per-feature check leaves that feature empty


def month_rows(pq: Path, month: str) -> pd.DataFrame:
    cols = sorted(set(LC.FEATURES) | {"permno", "month", "me_lag", "price_lag"})
    d = duckdb.connect().execute(f"SELECT {', '.join(cols)} FROM read_parquet('{pq.as_posix()}') WHERE in_investable AND month = DATE '{month}' ORDER BY permno").df()
    d["month"] = pd.to_datetime(d["month"])
    return d


def unaccepted_gates(doc: Mapping[str, Any], ptr: Mapping[str, Any]) -> list[str]:
    """Failed release gates that are neither owner-waived (pointer `owner_waivers`) nor per-feature parity gates."""
    gates = (doc.get("validation") or {}).get("gates") or {}
    if not gates:
        return ["release has no validation gates"]
    waived = {g for w in ptr.get("owner_waivers", []) for g in w.get("gates", [])}
    return sorted(g for g, v in gates.items() if v != "pass" and g not in waived and not g.startswith(PARITY_PREFIXES))


def input_coverage(d: pd.DataFrame) -> dict[str, float]:
    return {c: round(float(d[c].notna().mean()), 4) for c in LC.FEATURES}


def rail(rows: pd.DataFrame, ref: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Inputs whose coverage is more than MAX_COVERAGE_DROP below the reference (last engine month): {input: (coverage, reference)}; empty = pass; no rows fails every input."""
    cov = input_coverage(rows) if len(rows) else {c: 0.0 for c in LC.FEATURES}
    return {c: (v, ref[c]) for c, v in cov.items() if v < ref[c] - MAX_COVERAGE_DROP}


def main(argv: list[str] | None = None, pointer: Path = PT.POINTER) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="decision month, YYYY-MM-01")
    a = ap.parse_args(argv)
    stem = f"{a.month[:7]}__EXAMPLE"
    if (OUT / f"{stem}.parquet").exists():
        raise SystemExit(f"refused: {OUT / stem}.parquet exists; stored scores are the record a book was built from: move the file aside to rescore {a.month[:7]}")
    pq, ptr = PT.current(pointer)
    bad = unaccepted_gates(verify_release(pq.parent), ptr)
    if bad:
        raise SystemExit(f"refused: release gates failed and not accepted: {bad}")
    assert_frozen_intact()
    rows = month_rows(pq, a.month)
    ref = input_coverage(month_rows(pq, LAST_ENGINE_MONTH))
    short = rail(rows, ref)
    if short:
        raise SystemExit(f"refused: {len(rows)} rows; inputs more than {MAX_COVERAGE_DROP:.0%} below their {LAST_ENGINE_MONTH[:7]} coverage (coverage, reference): {short}")
    models, info = LC.fit()
    ranks = LC.seed_ranks(models, rows)
    out = rows[["permno", "me_lag", "price_lag"]].copy()
    out["score"] = ranks.mean(axis=0)
    out["q"] = out["score"].rank(pct=True)
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT / f"{stem}.parquet", index=False)
    rec = {"decision_month": a.month, "score": "EXAMPLE", "release": ptr["path"], "release_sha256": ptr["sha256"], "rows": int(len(out)), **info, "input_coverage": input_coverage(rows), "input_coverage_reference": ref, "seed_rank_corr_min": round(float(np.min(np.corrcoef(ranks))), 4),
           "status": "scores only: no account read, no order"}
    (OUT / f"{stem}.json").write_text(json.dumps(rec, indent=1) + "\n")
    print(json.dumps({k: v for k, v in rec.items() if k != "input_coverage"}, indent=1))


def latest() -> tuple[str, Path] | None:
    """Newest decision month with a score file, and its parquet."""
    fs = sorted(OUT.glob("*__EXAMPLE.parquet"))
    return (fs[-1].name.split("__")[0] + "-01", fs[-1]) if fs else None


if __name__ == "__main__":
    main()
