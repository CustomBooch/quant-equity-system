"""Append-only, hash-chained JSON lines: the one implementation of the record format of the PAPER audit log (src/quant/execution/audit.py), the experiment registry
(src/quant/core/registry/store.py).

Each line is one JSON object carrying `prev`, the SHA-256 of the line before it (null on the first line), so an edited, inserted, reordered or deleted line
breaks the chain at that line or the next. The SHA-256 of a line is the record's identity. Lines are appended and forced to disk before `append` returns, and
nothing is ever rewritten. Lines cut from the end leave a valid chain: such a cut shows only against a head recorded somewhere else.

    append(path, record, dumps)  add `prev`, write one line, fsync; (the record as written, the SHA-256 of its line)
    check(lines, valid=..., unchained_prefix=False)   which lines break the chain (1-based), how many leading records predate it, the head

The audit log (quant.execution.audit) and the registry store (quant.core.registry.store) keep their own code, because the bytes they write are a record; adopting it there is byte-neutral and is the owner's decision (the wiki page Architecture).
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PREV = "prev"
Record = dict[str, Any]


class ChainError(ValueError):
    """A record `append` refuses: it already carries `prev`, or its serialisation is not one line."""


def sha(data: bytes) -> str:
    """SHA-256 hex digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def lines(path: Path) -> list[bytes]:
    """The file's lines without their line ends; [] if it does not exist."""
    return path.read_bytes().splitlines() if path.exists() else []


def head(ls: Sequence[bytes]) -> str | None:
    """SHA-256 of the newest line, or None for an empty file."""
    return sha(ls[-1]) if ls else None


def compact(record: Mapping[str, Any]) -> str:
    """Canonical serialisation: sorted keys, no spaces, non-ASCII characters kept."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append(path: Path, record: Mapping[str, Any], dumps: Callable[[Record], str] = compact) -> tuple[Record, str]:
    """Append `record`, with `prev` set to the SHA-256 of the file's last line (None on the first), as one line serialised by `dumps`, and force it to disk
    before returning. `prev` is added after the record's own fields. Returns the record as written and the SHA-256 of its line. ChainError if the record
    already has `prev` or `dumps` writes more than one line."""
    if PREV in record:
        raise ChainError("the record already has `prev`: append sets it")
    full: Record = {**record, PREV: head(lines(path))}
    line = dumps(full).encode("utf-8")
    if b"\n" in line or b"\r" in line:
        raise ChainError("the serialised record is not a single line")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        f.write(line + b"\n")
        f.flush()
        os.fsync(f.fileno())
    return full, sha(line)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChainCheck:
    """The result of `check`: `broken` are 1-based line numbers, `unchained` the leading records written before the chain began."""
    records: int
    broken: tuple[int, ...]
    unchained: int
    head: str | None

    @property
    def ok(self) -> bool:
        return not self.broken


def _always(rec: Record) -> bool:
    return True


def check(ls: Sequence[bytes], *, valid: Callable[[Record], bool] = _always, unchained_prefix: bool = False) -> ChainCheck:
    """Check the chain of `ls`. A line is broken if it is not a JSON object, if `valid` rejects it, or if its `prev` is not the SHA-256 of the line before
    (None on the first line; a missing `prev` reads as None). With `unchained_prefix`, leading records without `prev` are counted as unchained instead (the
    PAPER audit log began unchained), and a record without `prev` after the chain began is broken."""
    broken: list[int] = []
    start: int | None = None
    for i, line in enumerate(ls):
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if not isinstance(rec, dict) or not valid(rec):
            broken.append(i + 1)
            continue
        if unchained_prefix and PREV not in rec:
            if start is not None:
                broken.append(i + 1)
            continue
        start = i if start is None else start
        if rec.get(PREV) != (sha(ls[i - 1]) if i else None):
            broken.append(i + 1)
    unchained = (len(ls) if start is None else start) if unchained_prefix else 0
    return ChainCheck(records=len(ls), broken=tuple(broken), unchained=unchained, head=head(ls))
