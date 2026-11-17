"""
config/credentials.py
======================
Central API key store.

ALL external API keys are loaded from environment variables here.
No key is ever hardcoded in a source file.

Usage
-----
    from config.credentials import FRED_API_KEY, EIA_API_KEY

    # Keys are strings when available, None when not set.
    # Callers must check for None and raise a clear error if the
    # specific key is required for the operation being attempted.

Environment variables
---------------------
The values live in the repository's .env, encrypted by dotenvx (https://dotenvx.com); the private key is in .env.keys (git-ignored, never shared). A command that
needs a key is started through dotenvx, which decrypts the file into that process's environment only:

    dotenvx run -- tools/py -m quant.data.wrds.verify_access

Nothing here reads the .env file itself. Names: FRED_API_KEY, EIA_API_KEY, EDGAR_USER_AGENT, WRDS_USERNAME, WRDS_PASSWORD. Add or change one with
`dotenvx set NAME value` (it is encrypted as it is written).

Security
--------
NEVER import this module and then pass its values into a string that
gets logged, printed, written to a file, or returned in an API response.
The keys are returned as strings intentionally so they can be passed
directly to HTTP request headers or library constructors — not for
display.

If you add a new data source that requires a key:
1. Add the key as a new constant here.
2. Add the corresponding environment variable name to the docstring.
3. Add an entry to the project .env.example file (with a placeholder).
4. Document the key source (where to register / obtain it).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("config.credentials")                       # the logger keeps the name it had before the move (the wiki page Architecture)

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def _get(env_var: str) -> str | None:
    """Read one environment variable, returning None if not set or empty. A value still in dotenvx's encrypted form means the .env was loaded without being
    decrypted: refuse rather than pass ciphertext on as a password."""
    val = os.environ.get(env_var, "").strip()
    if val.startswith("encrypted:"):
        raise OSError(f"{env_var} is still encrypted: start the command with `dotenvx run -- ...`")
    return val if val else None


# FRED (Federal Reserve Economic Data)
# Register at: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY: str | None = _get("FRED_API_KEY")

# EIA (U.S. Energy Information Administration)
# Register at: https://www.eia.gov/opendata/register.php
EIA_API_KEY: str | None = _get("EIA_API_KEY")

# EDGAR / SEC EDGAR Full-Text Search
# No API key currently required for EDGAR REST API (as of 2024).
# A User-Agent header identifying your application is required instead.
# See: https://www.sec.gov/os/accessing-edgar-data
# Set this to identify your application, e.g. "YourName your@email.com"
EDGAR_USER_AGENT: str | None = _get("EDGAR_USER_AGENT")

# WRDS (Wharton Research Data Services) — institutional account
# Passed to wrds.Connection(wrds_username=..., wrds_password=...); if unset, the wrds
# package prompts interactively (or uses ~/.pgpass / %APPDATA%\postgresql\pgpass.conf).
WRDS_USERNAME: str | None = _get("WRDS_USERNAME")
WRDS_PASSWORD: str | None = _get("WRDS_PASSWORD")


# ---------------------------------------------------------------------------
# Diagnostics (called at startup to surface missing keys early)
# ---------------------------------------------------------------------------

def check_credentials(required: list[str] | None = None) -> dict[str, bool]:
    """
    Check which credentials are available.

    Parameters
    ----------
    required
        Optional list of credential names that must be present.
        Raises EnvironmentError if any are missing.

    Returns
    -------
    dict
        Mapping of credential name → True/False (available or not).
    """
    status = {
        "FRED_API_KEY":    FRED_API_KEY is not None,
        "WRDS_USERNAME":   WRDS_USERNAME is not None,
        "WRDS_PASSWORD":   WRDS_PASSWORD is not None,
        "EIA_API_KEY":     EIA_API_KEY is not None,
        "EDGAR_USER_AGENT": EDGAR_USER_AGENT is not None,
    }

    if required:
        missing = [k for k in required if not status.get(k, False)]
        if missing:
            raise OSError(
                f"Required credentials not set: {missing}. "
                f"Add them with `dotenvx set NAME value` and start the command with `dotenvx run -- ...`. "
                f"See config/credentials.py for documentation."
            )

    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    status = check_credentials()
    print("Credential status:")
    for key, available in status.items():
        print(f"  {key:<25} {'✓ SET' if available else '✗ NOT SET'}")
