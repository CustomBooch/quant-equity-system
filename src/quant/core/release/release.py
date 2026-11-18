"""Write and verify a release directory under the hand-off contract contracts/RELEASE_V1.md (schema `release/1`).

A release is a directory of files plus VERSION.json, the manifest: the SHA-256 and size of every file, the inputs it was built from, the expected
figures, the commit and uncommitted-code state of this repository, and for data-layer releases a validation block of gates. A manifest is never
overwritten; a reader verifies the directory against it before reading anything (verify_release) and, for a data layer, requires every gate to pass
(require_validation). R (r/R/release.R) and Rust (`rust_core verify-release`) implement the same verification.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from quant.core.paths import REPO

SCHEMA = "release/1"
MANIFEST = "VERSION.json"


class ReleaseError(Exception):
    """A release is malformed, incomplete or has been altered."""


class FileRecord(TypedDict):
    """One listed file of a manifest."""
    sha256: str
    bytes: int


class CodeState(TypedDict):
    """Uncommitted code at the time of a release (code_state)."""
    dirty: bool
    files: list[str]
    sha256: str | None


class Manifest(TypedDict):
    """VERSION.json as write_release writes it (keys are serialised sorted)."""
    schema: str
    release: str
    description: str
    created_utc: str
    code_commit: str | None
    inputs: dict[str, str]
    files: dict[str, FileRecord]
    expected: dict[str, Any]
    code_dirty: NotRequired[bool]
    code_uncommitted_files: NotRequired[list[str]]
    code_state_sha256: NotRequired[str | None]
    validation: NotRequired[dict[str, Any]]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Hex SHA-256 of a file's bytes, read in chunks (the repository's one file hash)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def utc_stamp(now: dt.datetime | None = None) -> str:
    """`now` (the wall clock if None) as ISO-8601 UTC to the second, the form every manifest and pointer time takes. A naive time is refused: it
    names no instant."""
    t = dt.datetime.now(dt.timezone.utc) if now is None else now
    if t.tzinfo is None:
        raise ValueError(f"time {t.isoformat()} has no time zone; pass an aware UTC time")
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _listed(directory: Path) -> list[str]:
    """Every file under `directory` but the manifest, relative and sorted. A symbolic link anywhere in it is refused (amendment A21): it could point outside the release."""
    links = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_symlink())
    if links:
        raise ReleaseError(f"release holds symbolic links, which are refused: {links}")
    return sorted(rel for p in directory.rglob("*") if p.is_file() and (rel := p.relative_to(directory).as_posix()) != MANIFEST)


def _commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


# lab and panel are named from the record root (REPO, where git runs); the code folders from the top of the checkout (git's `:/`)
CODE_PATHSPEC = ["lab", ":/src", "panel", ":/tools", ":/contracts", ":/signal-repository/src", ":/r/R", ":/rust_core/src"]
CODE_SUFFIXES = (".py", ".sh", ".r", ".rs", ".toml", ".ps1", ".cmd")


def code_state() -> CodeState | None:
    """Uncommitted code at the time of a release: {'dirty': bool, 'files': [...], 'sha256': digest of the diff and the untracked code files}. None outside a git repository.
    Git runs in the record root and reports paths from the top of the checkout, so the answer does not depend on the directory the command was started from.

    A release records the commit it was built from; that is only true if the code was committed. The release of 2026-10-24g was built from a tree with uncommitted sealing code and recorded the
    commit before it, so rebuilding from the recorded commit would not have reproduced it.
    """
    try:
        out = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--", *CODE_PATHSPEC], cwd=REPO, text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    paths = [line[3:].strip().strip('"') for line in out.splitlines()]             # porcelain v1: two status letters, a space, the path ("old -> new" for a rename)
    files = sorted({p.split(" -> ")[-1] for p in paths if p.lower().endswith(CODE_SUFFIXES)})
    h = hashlib.sha256()
    if files:
        try:
            h.update(subprocess.check_output(["git", "diff", "HEAD", "--", *(":/" + f for f in files)], cwd=REPO, stderr=subprocess.DEVNULL))
        except (OSError, subprocess.CalledProcessError):
            pass                                           # no HEAD yet (a repository without commits): every code file is untracked and hashed below
        for f in files:                                    # untracked files are not in the diff: hash their content
            fp = REPO / f
            if fp.is_file():
                h.update(f.encode() + sha256_file(fp).encode())
    return {"dirty": bool(files), "files": files, "sha256": h.hexdigest() if files else None}


def write_release(directory: Path, release: str, description: str = "", inputs: dict[str, str] | None = None, expected: dict[str, Any] | None = None,
                  code_commit: str | None = None, validation: dict[str, Any] | None = None, allow_dirty_code: bool = False, *, now: dt.datetime | None = None) -> Manifest:
    """Hash every file currently in `directory` and write VERSION.json; return the manifest.

    `code_commit`: None records this repository's HEAD and refuses uncommitted code unless `allow_dirty_code` (the dirty state is then recorded);
    a given value is the caller's statement (tests, replays) and skips the check. `now`: the creation time (aware; default the wall clock).
    Raises ReleaseError if a manifest already exists (a release is immutable), the directory holds no file, or code is uncommitted and not allowed."""
    directory = Path(directory)
    manifest = directory / MANIFEST
    if manifest.exists():
        raise ReleaseError(f"{manifest} already exists; a release is immutable, write a new version")
    files: dict[str, FileRecord] = {rel: {"sha256": sha256_file(directory / rel), "bytes": (directory / rel).stat().st_size} for rel in _listed(directory)}
    if not files:
        raise ReleaseError("no files to release")
    state = code_state() if code_commit is None else None          # an explicit commit is the caller's statement (tests, replays); the check is for the recorded HEAD
    if state is not None and state["dirty"] and not allow_dirty_code:
        raise ReleaseError(f"{len(state['files'])} code file(s) have uncommitted changes (for example {state['files'][:3]}): commit them so that code_commit is true, or pass allow_dirty_code=True to record the dirty state")
    doc: Manifest = {"schema": SCHEMA, "release": release, "description": description, "created_utc": utc_stamp(now),
                     "code_commit": code_commit if code_commit is not None else _commit(), "inputs": inputs or {}, "files": files, "expected": expected or {}}
    if state is not None:
        doc["code_dirty"] = state["dirty"]
        if state["dirty"]:
            doc["code_uncommitted_files"] = state["files"]
            doc["code_state_sha256"] = state["sha256"]
    if validation is not None:
        doc["validation"] = validation
    manifest.write_bytes((json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8"))  # LF on every platform
    return doc


def verify_release(directory: Path) -> Manifest:
    """Return the manifest if every listed file matches and nothing unlisted is present; raise ReleaseError otherwise."""
    directory = Path(directory)
    try:
        doc: Manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReleaseError(f"cannot read {MANIFEST}: {e}") from e
    if doc.get("schema") != SCHEMA:
        raise ReleaseError(f"unsupported schema {doc.get('schema')!r}; expected {SCHEMA!r}")
    files = doc.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError("manifest lists no files")
    problems = []
    for rel, meta in sorted(files.items()):
        p = directory / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
        elif p.stat().st_size != meta["bytes"]:
            problems.append(f"size differs: {rel}")
        elif sha256_file(p) != meta["sha256"]:
            problems.append(f"hash differs: {rel}")
    problems += [f"not listed in manifest: {rel}" for rel in _listed(directory) if rel not in files]
    if problems:
        raise ReleaseError("; ".join(problems))
    return doc


def require_validation(doc: Manifest) -> Manifest:
    """For data-layer releases: the manifest must carry a validation report hash that matches a listed file, and every gate must be exactly "pass"."""
    v = doc.get("validation")
    if not isinstance(v, dict):
        raise ReleaseError("release has no validation block")
    gates = v.get("gates")
    if not isinstance(gates, dict) or not gates:
        raise ReleaseError("validation lists no gates")
    failed = sorted(k for k, g in gates.items() if g != "pass")
    if failed:
        raise ReleaseError(f"validation gates not passed: {failed}")
    if v.get("report_sha256") not in {m["sha256"] for m in doc["files"].values()}:
        raise ReleaseError("validation report hash is not that of a listed file")
    return doc
