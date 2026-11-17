"""The PAPER audit log, lab/paper/audit.jsonl (SPEC section 7, amendment A5): append-only JSON lines, one per plan, refusal, batch, order and reconciliation.
Each record carries `prev`, the SHA-256 of the line before it, so an edited, inserted, reordered or deleted line breaks the chain (`verify`). Records written before the
chain began have no `prev`; the first chained record covers them. Lines cut from the end leave a valid chain: compare `head` with the value preflight last reported."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

from quant.core.paths import REPO

PATH = REPO / "lab/paper/audit.jsonl"


def sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def lines() -> list[bytes]:
    return PATH.read_bytes().splitlines() if PATH.exists() else []


def records() -> list[dict[str, Any]]:
    return [json.loads(x) for x in lines()]


def log(event: str, **kw: Any) -> dict[str, Any]:
    """Append one record, chained to the line before it, and force it to disk before returning."""
    last = lines()[-1:]
    rec = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "event": event, **kw, "prev": sha(last[0]) if last else None}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with PATH.open("ab") as f:
        f.write(json.dumps(rec, default=str).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


def verify() -> dict[str, Any]:
    """records: lines in the log; unchained: leading records written before the chain began; broken: 1-based numbers of lines that are not a JSON object,
    lack `prev` after the chain began, or whose `prev` is not the hash of the line before; head: hash of the newest line."""
    ls, broken, start = lines(), [], None
    for i, line in enumerate(ls):
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            broken.append(i + 1)
            continue
        if "prev" not in rec:
            if start is not None:
                broken.append(i + 1)
            continue
        start = i if start is None else start
        if rec["prev"] != (sha(ls[i - 1]) if i else None):
            broken.append(i + 1)
    return {"records": len(ls), "unchained": len(ls) if start is None else start, "broken": broken, "head": sha(ls[-1]) if ls else None}
