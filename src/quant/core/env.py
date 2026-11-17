"""Environment values for credentials: one definition for every broker module."""
from __future__ import annotations

import os


def env(name: str) -> str:
    """Value of NAME in the process environment ('' if absent). The repository's .env is encrypted by dotenvx, so the keys exist only in a command started with
    `dotenvx run -- <command>`; a value still in its encrypted form means the file was loaded without being decrypted, and refuses. Never printed or logged."""
    v = os.environ.get(name, "").strip()
    if v.startswith("encrypted:"):
        raise RuntimeError(f"{name} is still encrypted: start the command with `dotenvx run -- ...`")
    return v
