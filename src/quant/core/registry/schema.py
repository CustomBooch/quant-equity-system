"""The experiment record (the wiki page Home): what one research run, candidate, configuration or test must state to be a first-class, immutable record.

Required: id, kind, campaign, verdict, trials, source. Optional: hypothesis, spec, config, seeds, split, data, code_commit, metrics, folds, selection_rule, supersedes, notes, extra.
`trials` is how many configurations the record stands for in the search-space count (1 for a candidate, the variants counted for a ledger row, 0 for a diagnostic or placebo, n for a bulk record
whose members were counted but not individually recorded). Unknown fields are refused so that a typo cannot silently drop information.
"""
from __future__ import annotations

import json
import re
from typing import Any

KINDS = ("experiment", "candidate", "configuration", "run", "test", "bulk")
VERDICTS = ("passed", "failed", "rejected", "void", "inconclusive", "descriptive", "completed", "placebo", "registered")
FAILED_VERDICTS = ("failed", "rejected", "void", "inconclusive")           # what the failed-experiment table lists (a placebo is a control, not a failure)
REQUIRED = ("id", "kind", "campaign", "verdict", "trials", "source")
OPTIONAL = ("hypothesis", "spec", "config", "seeds", "split", "data", "code_commit", "metrics", "folds", "selection_rule", "supersedes", "notes", "extra")
CHAINED = ("recorded_utc", "content_sha", "prev")                           # added by the store, never by the caller
SHA = re.compile(r"[0-9a-f]{64}")

Record = dict[str, Any]                                                      # one registry record, as JSON


def problems(rec: object) -> list[str]:
    """Every reason the record is not acceptable (empty when it is)."""
    out: list[str] = []
    if not isinstance(rec, dict):
        return ["the record is not an object"]
    out += [f"missing {k}" for k in REQUIRED if k not in rec]
    out += [f"unknown field {k}" for k in rec if k not in (*REQUIRED, *OPTIONAL, *CHAINED)]
    if "id" in rec and not (isinstance(rec["id"], str) and rec["id"] and not re.search(r"\s", rec["id"])):
        out.append("id must be a non-empty string without whitespace")
    if "kind" in rec and rec["kind"] not in KINDS:
        out.append(f"kind must be one of {KINDS}")
    if "verdict" in rec and rec["verdict"] not in VERDICTS:
        out.append(f"verdict must be one of {VERDICTS}")
    if "campaign" in rec and not (isinstance(rec["campaign"], str) and rec["campaign"]):
        out.append("campaign must be a non-empty string")
    if "trials" in rec and not (isinstance(rec["trials"], int) and not isinstance(rec["trials"], bool) and rec["trials"] >= 0):
        out.append("trials must be an integer of at least 0")
    src = rec.get("source")
    if "source" in rec and not (isinstance(src, dict) and isinstance(src.get("path"), str) and src["path"] and (src.get("sha256") is None or SHA.fullmatch(str(src["sha256"])))):
        out.append("source needs a path and, if given, a sha256")
    spec = rec.get("spec")
    if spec is not None and not (isinstance(spec, dict) and isinstance(spec.get("file"), str) and SHA.fullmatch(str(spec.get("sha256", "")))):
        out.append("spec needs a file and a sha256")
    data = rec.get("data")
    if data is not None and not (isinstance(data, dict) and all(isinstance(k, str) and SHA.fullmatch(str(v)) for k, v in data.items())):
        out.append("data must map names to sha256 hashes")
    seeds = rec.get("seeds")
    if seeds is not None and not (isinstance(seeds, list) and all(isinstance(s, int) and not isinstance(s, bool) for s in seeds)):
        out.append("seeds must be a list of integers")
    for k in ("config", "metrics", "folds", "extra"):
        if k in rec and not isinstance(rec[k], dict):
            out.append(f"{k} must be an object")
    try:
        json.dumps(rec, allow_nan=False)
    except (TypeError, ValueError) as e:
        out.append(f"not JSON-serialisable: {e}")
    return out
