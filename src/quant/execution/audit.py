"""The PAPER audit log, lab/paper/audit.jsonl (SPEC section 7, amendment A5): append-only JSON lines, one per plan, refusal, batch, order and reconciliation.
Each record carries `prev`, the SHA-256 of the line before it, so an edited, inserted, reordered or deleted line breaks the chain (`verify`). Records written before the
chain began have no `prev`; the first chained record covers them, and only those listed in PRE_CHAIN are accepted (amendment A15). Lines cut from the end leave a valid chain: compare `head` with the value preflight last reported."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from quant.core.paths import REPO

PATH = REPO / "lab/paper/audit.jsonl"
# Records written before the chain began (amendment A5), by the SHA-256 of their line (amendment A15); any other record without `prev` breaks the chain.
PRE_CHAIN: frozenset[str] = frozenset()                 # no record predates the chain here


def sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def lines() -> list[bytes]:
    return PATH.read_bytes().splitlines() if PATH.exists() else []


def records() -> list[dict[str, Any]]:
    return [json.loads(x) for x in lines()]


@contextmanager
def writer_lock() -> Iterator[None]:
    """One writer at a time (amendment A17): reading the last line and appending the next is one step, so two processes (a plan made while a send is running,
    a refused second send) cannot both chain to the same predecessor. An operating-system lock on a companion file, released if the process dies."""
    lock = PATH.with_name(PATH.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as f:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)          # waits about ten seconds, then raises OSError: nothing is written unchained
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def log(event: str, **kw: Any) -> dict[str, Any]:
    """Append one record, chained to the line before it, and force it to disk before returning."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock():
        last = lines()[-1:]
        rec = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "event": event, **kw, "prev": sha(last[0]) if last else None}
        with PATH.open("ab") as f:
            f.write(json.dumps(rec, default=str).encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())
    return rec


def anchor_problem() -> str | None:
    """Why the log is not the one git last committed, or None (amendment A16). The chain proves the lines agree with each other, not that they are the
    lines that were written; the committed copy is the anchor outside the file: every committed line must still be there, unchanged and in place. None when
    the log is not tracked by git, git cannot be run here, or nothing is committed yet (a check that cannot run is not a failed check)."""
    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=PATH.parent, capture_output=True, check=False, timeout=60)

    try:
        if run("ls-files", "--error-unmatch", "--", PATH.name).returncode:
            return None
        shown = run("show", f"HEAD:./{PATH.name}")
        if shown.returncode:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    committed, now = shown.stdout.splitlines(), lines()
    if len(now) < len(committed):
        return f"the log has {len(now)} lines but git last committed {len(committed)}: lines were removed since the last commit"
    for i, line in enumerate(committed):
        if now[i] != line:
            return f"line {i + 1} differs from the line git last committed: the log was changed after it was committed"
    return None


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
            if start is not None or sha(line) not in PRE_CHAIN:
                broken.append(i + 1)
            continue
        start = i if start is None else start
        if rec["prev"] != (sha(ls[i - 1]) if i else None):
            broken.append(i + 1)
    return {"records": len(ls), "unchained": len(ls) if start is None else start, "broken": broken, "head": sha(ls[-1]) if ls else None,
            "anchor_problem": anchor_problem()}
