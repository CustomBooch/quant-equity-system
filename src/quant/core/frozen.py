"""The frozen-artefact check: every file the freeze recorded (lab/prospective/frozen/FREEZE.json) still has its recorded sha256.

Scoring refuses when it fails (`assert_frozen_intact`, quant.models.scores) and preflight reports it (`check_frozen`, quant.execution.preflight). The same check as
`lab.core.c5_audit` (the research programme's stage-A audit, which keeps its own copy for its records); tools/tests/test_duplicate_parity.py checks the two agree.
It reads the freeze file as data and imports nothing from the research record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from quant.core.paths import REPO

FREEZE = REPO / "lab/prospective/frozen/FREEZE.json"
GROUPS = ("code", "v0_artefacts", "historical_predictions")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 22):
            h.update(b)
    return h.hexdigest()


def frozen_entries(freeze: Path | None = None) -> dict[str, str]:
    """{repository-relative path: sha256} of every frozen file: the code, the v0 artefacts, the historical predictions and the hashed data snapshots."""
    path = freeze or FREEZE
    if not path.is_file() and not path.parent.exists():      # no freeze folder at all: nothing has been frozen in this checkout
        return {}
    f = json.loads(path.read_text())
    ent: dict[str, str] = {}
    for grp in GROUPS:
        ent.update(f[grp])
    for k, v in f.get("data_snapshots", {}).items():
        if isinstance(v, str) and len(v) == 64:
            ent[k] = v
    return ent


def check_frozen(freeze: Path | None = None) -> dict[str, Any]:
    """{entries_checked, mismatches, missing, intact}."""
    ent = frozen_entries(freeze)
    bad, missing = [], []
    for rel, h in ent.items():
        p = REPO / rel
        if not p.exists():
            missing.append(rel)
        elif sha256(p) != h:
            bad.append(rel)
    return {"entries_checked": len(ent), "mismatches": bad, "missing": missing, "intact": not bad and not missing}


def assert_frozen_intact(freeze: Path | None = None) -> None:
    r = check_frozen(freeze)
    if not r["intact"]:
        raise RuntimeError(f"frozen artefacts changed or missing: {r}")
