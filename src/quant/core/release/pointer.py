"""The master pointer (panel/MASTER_CURRENT.json): which master release is current, and promotion of a new one.

    python -m quant.core.release.pointer show
    python -m quant.core.release.pointer promote data/master/<release> --note "..." --previous-note "..."

Promotion verifies the release by hash, records its file hash, size, rows, columns and gate summary, carries the owner waivers forward (only for gates that exist in the new
release), moves the old current entry to `previous_release` with the given note, and never deletes a file. Waivers are the owner's: this module never adds one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import duckdb

from quant.core.paths import REPO
from quant.core.release.release import ReleaseError, sha256_file, utc_stamp, verify_release
from quant.core.sql import one_row, quote

POINTER = REPO / "panel/MASTER_CURRENT.json"
PANEL_FILE = "master_panel.parquet"

Entry = dict[str, Any]                      # one release entry of the pointer (path, release, sha256, bytes, rows, columns, months, gates, owner_waivers, ...)


def read(pointer: Path = POINTER) -> dict[str, Any]:
    """The pointer file; the owner edits its waivers by hand, so a byte-order mark from the editor is accepted."""
    doc: dict[str, Any] = json.loads(pointer.read_text(encoding="utf-8-sig"))
    return doc


def current(pointer: Path = POINTER) -> tuple[Path, Entry]:
    """(absolute path of the current master parquet, its pointer entry)."""
    entry: Entry = read(pointer)["current"]
    return REPO / entry["path"], entry


def repo_relative(p: Path) -> str:
    """`p` resolved, as a POSIX path relative to the repository root, or absolute if it lies outside the repository."""
    r = p.resolve()
    return r.relative_to(REPO).as_posix() if r.is_relative_to(REPO) else r.as_posix()


def _one_row(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[Any, ...]:
    return one_row(con, sql, ReleaseError)


def entry_for(release_dir: Path, note: str, waivers: list[dict[str, Any]], code_commit: str | None = None, *, now: dt.datetime | None = None) -> Entry:
    """The pointer entry of a release: verified by hash first (ReleaseError if it fails), then the panel file's hash, size, rows, columns and month range,
    the gate summary, and the owner waivers that name only gates this release has. `now`: the promotion time (aware; default the wall clock)."""
    doc = verify_release(release_dir)
    pq = release_dir / PANEL_FILE
    with duckdb.connect() as con:
        rows = _one_row(con, f"SELECT count(*) FROM read_parquet({quote(pq.as_posix())})")[0]
        cols = len(con.execute(f"DESCRIBE SELECT * FROM read_parquet({quote(pq.as_posix())})").fetchall())
        months = _one_row(con, f"SELECT min(month), max(month) FROM read_parquet({quote(pq.as_posix())})")
    gates = (doc.get("validation") or {}).get("gates") or {}
    return {"path": repo_relative(pq), "release": repo_relative(release_dir / "VERSION.json"), "sha256": sha256_file(pq), "bytes": pq.stat().st_size, "rows": int(rows), "columns": cols,
            "months": f"{str(months[0])[:7]} to {str(months[1])[:7]}", "code_commit": code_commit or doc.get("code_commit"),
            "promoted_utc": utc_stamp(now), "note": note,
            "gates": {"total": len(gates), "pass": sum(v == "pass" for v in gates.values()), "fail": sorted(k for k, v in gates.items() if v != "pass")},
            "owner_waivers": [w for w in waivers if set(w.get("gates", [])) <= set(gates)]}


def promote(release_dir: Path, note: str, previous_note: str, pointer: Path = POINTER, code_commit: str | None = None, *, now: dt.datetime | None = None) -> Entry:
    """Make `release_dir` current: its entry (entry_for, carrying the current entry's waivers) becomes `current` and the old current entry becomes
    `previous_release` with `previous_note`. SystemExit if the release is already current; nothing is deleted."""
    ptr = read(pointer)
    old = ptr["current"]
    new = entry_for(release_dir, note, old.get("owner_waivers", []), code_commit, now=now)
    if new["sha256"] == old["sha256"]:
        raise SystemExit("this release is already current")
    ptr["previous_release"] = {**old, "note": previous_note}
    ptr["current"] = new
    pointer.write_text(json.dumps(ptr, indent=1) + "\n", encoding="utf-8")
    return new


def head_commit() -> str | None:
    """Abbreviated HEAD commit of this repository, or None outside a git repository."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None, pointer: Path = POINTER) -> None:
    """`show` prints the current entry; `promote <dir> --note ... --previous-note ...` promotes a release with this repository's commit."""
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    p = sub.add_parser("promote")
    p.add_argument("release_dir")
    p.add_argument("--note", required=True, help="what the new release is (and where it is documented)")
    p.add_argument("--previous-note", required=True, help="why the old current release is kept (rollback note)")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        print(json.dumps(read(pointer)["current"], indent=1))
        return
    print(json.dumps(promote(Path(a.release_dir), a.note, a.previous_note, pointer, code_commit=head_commit()), indent=1))


if __name__ == "__main__":
    main()
