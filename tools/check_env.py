"""Compare the installed Python packages with requirements/full.lock.txt and say what differs.

    python tools/check_env.py                                      # check against requirements/full.lock.txt; report; exit 0
    python tools/check_env.py --lock requirements/core.lock.txt    # check against the core lock
    python tools/check_env.py --strict                             # exit 1 if anything required is missing or differs

Pins that cannot be installed from PyPI are listed in KNOWN_UNOBTAINABLE with the reason; they are reported but never fail the check.
Read-only: installs nothing, no network.
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "requirements" / "full.lock.txt"

# package -> why the pin cannot be reproduced with pip from PyPI
KNOWN_UNOBTAINABLE = {
    "torch": "pinned to a CUDA 12.8 nightly (2.12.0.dev20260408+cu128) that is not on PyPI; a released torch built for cu128 is the practical substitute (see the wiki page Home)",
}


def norm(name: str) -> str:
    """A package name normalised as pip compares them (runs of -, _ and . are one -, lower case)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: Path) -> dict[str, str]:
    """{normalised name: pinned version} of every `name==version` line of a lock file; comments and other lines are skipped."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)", line.strip())
        if m:
            pins[norm(m.group(1))] = m.group(2)
    return pins


def compare(pins: dict[str, str], installed: dict[str, str]) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str, str]], list[tuple[str, str, str | None]]]:
    """(matching names, missing (name, pin), differing (name, pin, installed), known-unobtainable (name, pin, installed or None)), each sorted by name."""
    ok: list[str] = []
    missing: list[tuple[str, str]] = []
    differ: list[tuple[str, str, str]] = []
    unobtainable: list[tuple[str, str, str | None]] = []
    for name, want in sorted(pins.items()):
        have = installed.get(name)
        if name in KNOWN_UNOBTAINABLE:
            unobtainable.append((name, want, have))
        elif have is None:
            missing.append((name, want))
        elif have != want:
            differ.append((name, want, have))
        else:
            ok.append(name)
    return ok, missing, differ, unobtainable


def main(argv: list[str] | None = None) -> int:
    """Print what differs between the environment and the lock; with --strict, 1 if a pin is missing or differs (known-unobtainable pins never fail)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--lock", default=str(LOCK), help="lock file to compare with (default: requirements/full.lock.txt)")
    args = ap.parse_args(argv)
    strict = args.strict
    pins = read_lock(Path(args.lock))
    installed = {norm(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}
    ok, missing, differ, unobtainable = compare(pins, installed)
    print(f"{Path(args.lock).name}: {len(pins)} pins | match {len(ok)} | differ {len(differ)} | missing {len(missing)} | known-unobtainable {len(unobtainable)}")
    for n, w, h in differ:
        print(f"  DIFFERS  {n}: lock {w}, installed {h}")
    for n, w in missing:
        print(f"  MISSING  {n}=={w}")
    for n, w, have in unobtainable:
        print(f"  NOTE     {n}: lock {w}, installed {have or 'none'} -- {KNOWN_UNOBTAINABLE[n]}")
    return 1 if strict and (differ or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
