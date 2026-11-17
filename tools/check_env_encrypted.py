"""The repository's secrets stay encrypted: every value in .env is a dotenvx ciphertext, and .env and its private key .env.keys are git-ignored.

    tools/py tools/check_env_encrypted.py            # the verdict; exit 1 on any finding (no .env at all is fine: a fresh clone has none)

A plaintext value appears when a key is added by editing the file instead of `dotenvx set NAME value`; re-encrypt with `dotenvx encrypt`. Values are never printed,
only the names of the variables that are not encrypted.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PUBLIC = re.compile(r"^DOTENV_PUBLIC_KEY")                     # the public key is meant to be readable


def plaintext(text: str) -> list[str]:
    """Names of the variables whose value is not a dotenvx ciphertext (empty values count: an empty secret is written in plain)."""
    out = []
    for ln in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", ln)
        if not m or PUBLIC.match(m.group(1)):
            continue
        if not m.group(2).strip().strip('"').strip("'").startswith("encrypted:"):
            out.append(m.group(1))
    return out


def not_ignored(repo: Path, names: tuple[str, ...] = (".env", ".env.keys")) -> list[str]:
    """Which of `names` git would track (git check-ignore exits 1 for a path it does not ignore)."""
    return [n for n in names if subprocess.run(["git", "check-ignore", "-q", n], cwd=repo, check=False).returncode != 0]


def findings(repo: Path = REPO) -> list[str]:
    """One line per plaintext .env value (by variable name, never the value) and per secret file git would track; [] when the secrets are safe."""
    env = repo / ".env"
    out = [f".env: {n} is not encrypted (use `dotenvx set {n} <value>` or `dotenvx encrypt`)" for n in (plaintext(env.read_text(encoding="utf-8")) if env.exists() else [])]
    out += [f"{n} is not git-ignored" for n in not_ignored(repo)]
    return out


def main() -> int:
    """Print the findings or the all-clear line; 1 on any finding, else 0."""
    f = findings()
    print("\n".join(f) if f else "secrets: every .env value encrypted; .env and .env.keys git-ignored")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
