"""Alpaca paper-account access (PAPER amendment A10): the host, the keys and read-only GET. No order function lives here; the only module that may
send an order is quant.execution.engine, through its gates."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from quant.core.env import env
from quant.execution.brokers.http import secret_get

PAPER = "https://paper-api.alpaca.markets"                   # paper account only; no other host is ever called


def headers() -> dict[str, str]:
    """Alpaca key headers from ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET; refuses before any request when either is missing."""
    key, secret = env("ALPACA_PAPER_KEY_ID"), env("ALPACA_PAPER_SECRET")
    if not key or not secret:
        raise RuntimeError("ALPACA_PAPER_KEY_ID or ALPACA_PAPER_SECRET is not set: start the command with `dotenvx run -- ...` (keys are in the encrypted .env)")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def get(path: str) -> Any:
    """GET PAPER + path (path must start with /v2/); parsed JSON. HTTP errors raise."""
    if not path.startswith("/v2/"):
        raise ValueError(f"unexpected path {path!r}: only /v2/ GETs are allowed")
    req = secret_get(PAPER + path, headers(), {"User-Agent": "quant-research-readonly"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def account() -> dict[str, Any]:
    out: dict[str, Any] = get("/v2/account")
    return out


def account_problems(acct: dict[str, Any]) -> list[str]:
    """Reasons the account cannot be traded under A10 (empty list = fine): status ACTIVE, not blocked, USD."""
    out = []
    if acct.get("status") != "ACTIVE":
        out.append(f"account status {acct.get('status')!r}, not ACTIVE")
    for k in ("trading_blocked", "account_blocked"):
        if acct.get(k) is True:
            out.append(f"{k} is true")
    if acct.get("currency") != "USD":
        out.append(f"account currency {acct.get('currency')!r}, not USD")
    return out


def equity_usd(acct: dict[str, Any]) -> float:
    """Account equity in USD (A10: no FX conversion)."""
    v = float(acct["equity"])
    if not v > 0:
        raise ValueError(f"account equity {v} is not positive")
    return v


def positions() -> dict[str, float]:
    """Open positions, symbol -> signed quantity (short positions negative)."""
    out = {}
    for p in get("/v2/positions"):
        q = float(p["qty"])
        out[p["symbol"]] = -abs(q) if p.get("side") == "short" else q
    return out


def position_values() -> dict[str, tuple[float, float | None]]:
    """Open positions, symbol -> (signed quantity, signed market value in USD or None when the broker did not give one)."""
    out: dict[str, tuple[float, float | None]] = {}
    for p in get("/v2/positions"):
        short = p.get("side") == "short"
        q = float(p["qty"])
        mv = p.get("market_value")
        out[p["symbol"]] = (-abs(q) if short else q, None if mv is None else (-abs(float(mv)) if short else float(mv)))
    return out


def asset(symbol: str) -> dict[str, Any]:
    if not symbol or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ." for c in symbol):
        raise ValueError(f"bad symbol {symbol!r}")
    out: dict[str, Any] = get(f"/v2/assets/{symbol}")
    return out


def clock() -> dict[str, Any]:
    out: dict[str, Any] = get("/v2/clock")
    return out
