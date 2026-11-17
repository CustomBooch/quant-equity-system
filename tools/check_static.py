"""Static analysis of the production path: ruff's defect and security rules on every module the monthly commands load (tools/check_imports.py).

    tools/py tools/check_static.py            # findings, then the verdict; exit 1 on any finding
    tools/py tools/check_static.py FILE ...   # the same rules on other files

Rules: pyflakes (F), syntax (E9), bugbear (B), the bandit security set (S), pylint errors (PLE), subprocess without `check` (PLW1510) and files opened outside a
`with` (SIM115). A rule class is ignored only with its reason in IGNORED; a single line only with an inline `# noqa: <code> - <reason>`. The lab.core copies are
left out: they are byte copies of frozen originals (lab/core/tests/test_copies.py), so a finding there is one in the frozen code, which is never edited.
"""
from __future__ import annotations

import re
import subprocess
import sys

import check_imports as CI

SELECT = "F,E9,B,S,PLE,PLW1510,SIM115"
IGNORED = {
    "S101": "assert in tests is how pytest checks; production modules contain none (explicit raises instead, so checks survive python -O)",
    "S310": "urlopen is called only on quant.execution.brokers.t212.api.DEMO, a fixed https host (tested in src/quant/execution/brokers/t212/tests/test_api.py)",
    "S314": "the parsed XML is the inside of the owner's own Capital IQ .xlsx exports; Python's ElementTree does not resolve external entities",
    "S603": "subprocess argument lists are fixed (git, cargo, this interpreter); no shell and no outside input",
    "S607": "git and cargo are found on PATH by design (tools/py and checks.ps1 set it)",
    "S608": "DuckDB SQL is built from module constants, column names read from the file's own schema and integer ids; no outside input",
}
COPIES = "lab.core."
FINDING = re.compile(r"^.+?:\d+:\d+: ")                     # ruff's concise line `path:line:col: CODE message`; the path may be absolute (C:\..., /tmp/...)


def production_files() -> list[str]:
    """Repository-relative paths of the modules the production commands load (tools/check_imports.py), without the lab.core copies."""
    seen, _ = CI.closure(CI.ROOTS)
    return sorted(CI.module_file(m).relative_to(CI.REPO).as_posix() for m in seen if not m.startswith(COPIES))


def findings(files: list[str]) -> list[str]:
    """One line per finding, `path:line:col: CODE message`; [] when clean."""
    r = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated", "--select", SELECT, "--ignore", ",".join(IGNORED), "--output-format", "concise", *files],
                       cwd=CI.REPO, capture_output=True, text=True, check=False)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"ruff failed: {r.stderr.strip()[:500]}")
    return [x for x in r.stdout.splitlines() if FINDING.match(x)]


def main(argv: list[str]) -> int:
    """Print the findings on `argv` (default: the production path) and a summary line; 1 on any finding, else 0."""
    files = argv or production_files()
    out = findings(files)
    print("\n".join(out))
    print(f"{len(files)} files, {len(out)} findings ({SELECT}; ignored with reasons: {', '.join(IGNORED)})")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
