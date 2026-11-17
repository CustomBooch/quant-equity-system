"""Trading 212 practice-account access: the host, the keys and read-only GET. The only module that sends orders is quant.execution.engine, and only through its gates."""
from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any

from quant.core.env import env

DEMO = "https://demo.trading212.com/api/v0"                 # practice account; no other host is ever called


def auth(kind: str) -> str:
    """HTTP Basic header for the key pair T212_<kind>_API_KEY / T212_<kind>_SECRET_KEY; kind is READ (GET only) or FULL (orders). Refuses before any request when
    either key is missing."""
    key, secret = env(f"T212_{kind}_API_KEY"), env(f"T212_{kind}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"T212_{kind}_API_KEY or T212_{kind}_SECRET_KEY is not set: start the command with `dotenvx run -- ...` (keys are in the encrypted .env)")
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


def get_bytes(path: str) -> bytes:
    """GET DEMO + path with the read-only keys; the response body. HTTP errors raise."""
    req = urllib.request.Request(DEMO + path, headers={"Authorization": auth("READ"), "User-Agent": "quant-research-readonly"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body: bytes = r.read()
        return body


def get(path: str) -> Any:
    """GET DEMO + path with the read-only keys; parsed JSON."""
    return json.loads(get_bytes(path))


def positions() -> dict[str, float]:
    """Open positions of the practice account, Trading 212 ticker -> quantity. Accepts a list or {"items": [...]}, and the ticker at the top level or under
    "instrument"; a position without a ticker or quantity raises (the shape has not yet been seen on a non-empty account)."""
    pos = get("/equity/positions")
    return {(p.get("instrument") or {}).get("ticker") or p["ticker"]: float(p["quantity"]) for p in (pos if isinstance(pos, list) else pos["items"])}
