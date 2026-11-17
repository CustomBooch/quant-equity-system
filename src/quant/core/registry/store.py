"""The experiment registry file, lab/registry/experiments.jsonl: append-only JSON lines, one record each, chained like the PAPER audit log (src/quant/execution/audit.py; the format is the same, the
code is separate so that the production path is not touched). Each record carries `prev`, the SHA-256 of the line before it, so an edited, inserted, reordered or deleted line breaks the chain;
`content_sha` fingerprints the record's own content so that a re-import can tell "already recorded" from "changed". A correction is a new record whose `supersedes` names the old one."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict

from quant.core.paths import REPO
from quant.core.registry import schema
from quant.core.registry.schema import Record

PATH = REPO / "lab/registry/experiments.jsonl"


class RegistryError(Exception):
    """A record the registry refuses: invalid, a repeated id, or a `supersedes` naming no record."""


class Verification(TypedDict):
    records: int
    broken: list[int]
    invalid: dict[int, list[str]]
    duplicate_ids: list[str]
    bad_supersedes: list[str]
    head: str | None
    ok: bool


def sha(b: bytes) -> str:
    """SHA-256 hex digest of `b`."""
    return hashlib.sha256(b).hexdigest()


def content_sha(rec: Record) -> str:
    """Fingerprint of a record's content: the canonical JSON of every field except those the store adds."""
    body = {k: v for k, v in rec.items() if k not in schema.CHAINED}
    return sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def lines(path: Path = PATH) -> list[bytes]:
    """The registry file's lines (none if it does not exist)."""
    return Path(path).read_bytes().splitlines() if Path(path).exists() else []


def records(path: Path = PATH) -> list[Record]:
    """Every record in the file, in order (the chain is not checked: that is `verify`)."""
    return [json.loads(x) for x in lines(path)]


def active(recs: list[Record]) -> list[Record]:
    """The records no later record supersedes."""
    gone = {r["supersedes"] for r in recs if r.get("supersedes")}
    return [r for r in recs if r["id"] not in gone]


def append(rec: Record, path: Path = PATH, now: str | None = None) -> Record:
    """Validate and append one record, chained to the line before it, forced to disk before returning. Refuses an invalid record, a repeated id, and a `supersedes` that names no record."""
    bad = schema.problems(rec)
    if bad:
        raise RegistryError(f"{rec.get('id', '?')}: " + "; ".join(bad))
    existing = records(path)
    ids = {r["id"] for r in existing}
    if rec["id"] in ids:
        raise RegistryError(f"{rec['id']}: id already recorded (a correction is a new id with `supersedes`)")
    if rec.get("supersedes") and rec["supersedes"] not in ids:
        raise RegistryError(f"{rec['id']}: supersedes {rec['supersedes']}, which is not recorded")
    last = lines(path)[-1:]
    full = {**rec, "recorded_utc": now or dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "content_sha": content_sha(rec), "prev": sha(last[0]) if last else None}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("ab") as f:
        f.write(json.dumps(full, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    return full


def verify(path: Path = PATH) -> Verification:
    """records: lines; broken: 1-based lines that are not a JSON object or whose `prev` is not the hash of the line before; invalid: {line: problems} of records failing the schema or whose
    `content_sha` does not match their content; duplicate_ids; bad_supersedes: ids naming a record that does not come earlier; head: hash of the newest line."""
    ls = lines(path)
    broken: list[int] = []
    invalid: dict[int, list[str]] = {}
    seen: set[object] = set()
    dup: list[str] = []
    bad_sup: list[str] = []
    for i, line in enumerate(ls):
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            broken.append(i + 1)
            continue
        if rec.get("prev") != (sha(ls[i - 1]) if i else None):
            broken.append(i + 1)
        p = schema.problems(rec)
        if rec.get("content_sha") != content_sha(rec):
            p.append("content_sha does not match the content")
        if p:
            invalid[i + 1] = p
        if rec.get("id") in seen:
            dup.append(rec["id"])
        if rec.get("supersedes") and rec["supersedes"] not in seen:
            bad_sup.append(rec["id"])
        seen.add(rec.get("id"))
    return {"records": len(ls), "broken": broken, "invalid": invalid, "duplicate_ids": dup, "bad_supersedes": bad_sup, "head": sha(ls[-1]) if ls else None,
            "ok": not (broken or invalid or dup or bad_sup)}
