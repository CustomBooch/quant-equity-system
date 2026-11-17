"""The current master release on this machine agrees with its pointer (panel/MASTER_CURRENT.json). Skipped where the release is not present.

The fast tests compare the pointer with the release's VERSION.json (size, hash, gates, waivers, rollback entry). The full re-hash of the panel file (about 1.2 GB) is
marked `integration` and runs with `pytest -m integration lab/release/tests/test_current_release.py`; the score rail in quant.models.scores re-hashes it on every use anyway.
"""
from __future__ import annotations

import json

import pytest

from quant.core.release import pointer as PT
from quant.core.release.release import sha256_file, verify_release

REPO = PT.POINTER.parents[1]


@pytest.fixture(scope="module")
def ptr(require_data):
    p = PT.read()
    require_data(REPO / p["current"]["path"])
    return p


def _version(entry):
    return json.loads((REPO / entry["release"]).read_text())


def test_pointer_names_the_file_listed_in_the_release(ptr):
    cur = ptr["current"]
    path = REPO / cur["path"]
    v = _version(cur)
    listed = v["files"][path.name]
    assert (listed["sha256"], listed["bytes"]) == (cur["sha256"], cur["bytes"])
    assert path.stat().st_size == cur["bytes"]
    assert (REPO / cur["release"]).parent == path.parent


def test_pointer_gate_summary_matches_the_release_validation(ptr):
    cur = ptr["current"]
    gates = _version(cur)["validation"]["gates"]
    failed = sorted(k for k, s in gates.items() if s != "pass")
    assert cur["gates"]["total"] == len(gates)
    assert cur["gates"]["pass"] == len(gates) - len(failed)
    assert sorted(cur["gates"]["fail"]) == failed


def test_waivers_cover_only_failed_gates_and_are_the_owners(ptr):
    cur = ptr["current"]
    for w in cur.get("owner_waivers", []):
        assert w["by"] == "owner" and w["on"]
        assert set(w["gates"]) <= set(cur["gates"]["fail"]), w


def test_current_release_is_accepted_by_the_score_rail(ptr):
    """Every failed gate is either waived by the owner or a per-feature parity gate (quant.models.scores.unaccepted_gates)."""
    from quant.models import scores as SC

    cur = ptr["current"]
    gates = _version(cur)["validation"]["gates"]
    assert SC.unaccepted_gates({"validation": {"gates": gates}}, cur) == []


def test_rollback_entry_still_resolves(ptr):
    prev = ptr.get("previous_release")
    assert prev, "pointer has no rollback entry"
    v = _version(prev)
    name = (REPO / prev["path"]).name
    assert (v["files"][name]["sha256"], v["files"][name]["bytes"]) == (prev["sha256"], prev["bytes"])
    assert (REPO / prev["path"]).stat().st_size == prev["bytes"]


@pytest.mark.integration
def test_current_release_verifies_by_hash(ptr):
    cur = ptr["current"]
    assert sha256_file(REPO / cur["path"]) == cur["sha256"]
    verify_release((REPO / cur["path"]).parent)
