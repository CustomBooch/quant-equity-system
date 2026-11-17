"""PAPER order plans (lab/paper/SPEC.md section 4): what one Tuesday slot should trade. No account or broker access; quant.execution.engine sends a plan only through its gates.

    plan_from_target   a target book of quant.portfolio.targets, priced at Yahoo's latest close, for one slot's tranche, from the tranche's held shares (the account is not read)
    book_exposure      the overlay exposure the target book recorded (quant.portfolio.targets), the default exposure of a plan
    make_plan          the orders that move held shares to target shares, under the order rules below
    write_plan         stores the plan under its hash and records it in the audit log (the engine refuses a plan without that record)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from quant.execution import audit as A
from quant.core.paths import REPO

ROOT = REPO / "lab/paper"
PLANS = ROOT / "plans"
MIN_ORDER_USD, QTY_DECIMALS = 5.0, 4                        # SPEC section 4: skip orders below USD 5; whole or four-decimal fractional quantities


def plan_hash(orders: list[dict[str, Any]], account_value_usd: float) -> str:
    return hashlib.sha256(json.dumps({"orders": orders, "account_value_usd": round(account_value_usd, 2)}, sort_keys=True).encode()).hexdigest()


def plan_seal(plan: dict[str, Any]) -> str:
    """SHA-256 of the whole plan as canonical JSON, recorded with the plan in the audit log (amendment A15): a later change to any field shows."""
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def make_plan(target: dict[str, float], held: dict[str, float], prices: dict[str, float], account_value_usd: float, tranche_fraction: float = 1.0) -> dict[str, Any]:
    """Orders that move `held` (ticker -> shares) a fraction of the way to `target` (ticker -> shares). Sells use negative quantity. Rules of SPEC section 4."""
    orders: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for t in sorted(set(target) | set(held)):
        d = round((target.get(t, 0.0) - held.get(t, 0.0)) * tranche_fraction, QTY_DECIMALS)
        px = prices.get(t)
        if d == 0 or px is None:
            if d != 0:
                skipped.append({"ticker": t, "reason": "no_price"})
            continue
        usd = abs(d) * px
        if usd < MIN_ORDER_USD:
            skipped.append({"ticker": t, "reason": "below_min_order", "usd": round(usd, 2)})
            continue
        orders.append({"ticker": t, "quantity": d, "usd": round(usd, 2), "side": "BUY" if d > 0 else "SELL"})
    return {"orders": orders, "skipped": skipped, "account_value_usd": account_value_usd, "plan_hash": plan_hash(orders, account_value_usd)}


def write_plan(plan: dict[str, Any]) -> Path:
    """Store the plan under its hash and record it in the audit log; `send` refuses a plan without this record."""
    PLANS.mkdir(parents=True, exist_ok=True)
    f = PLANS / f"{plan['plan_hash']}.json"
    f.write_text(json.dumps(plan, indent=1) + "\n")
    A.log("plan", plan_hash=plan["plan_hash"], file=str(f), orders=len(plan["orders"]), slot=plan.get("slot"), tranche=plan.get("tranche"), broker=plan.get("broker", ""),
          plan_sha256=plan_seal(json.loads(f.read_text())))
    return f


def book_exposure(target_csv: Path) -> float:
    """The overlay exposure recorded with a target book (`overlay_state.exposure_now` in the .json beside the .csv); raises if the record is missing."""
    return float(json.loads(Path(target_csv).with_suffix(".json").read_text())["overlay_state"]["exposure_now"])


def latest_prices(tickers: list[str]) -> tuple[dict[str, float], str]:
    """Latest daily closes from Yahoo (public data, read-only) and the newest close date."""
    import yfinance as yf

    px = yf.download(tickers, period="5d", auto_adjust=False, progress=False)["Close"].ffill()
    last = px.iloc[-1]
    return {t: float(v) for t, v in last.items() if v == v and v > 0}, str(px.index[-1].date())


def plan_from_target(target_csv: Path, account_value_usd: float, exposure: float, slot: dt.date, held: dict[str, float] | None = None) -> dict[str, Any]:
    """Plan for one Tuesday slot from a target book (quant.portfolio.targets): that slot's tranche (1/K of the account times the overlay exposure) moves from `held` to the book.
    `held` is the tranche's current shares by Alpaca symbol (the engine passes the tranche ledger's); None means an empty tranche. The plan records it, so that a
    clean reconciliation can record the tranche's new holdings (held plus the orders the broker accepted)."""
    import pandas as pd

    from quant.portfolio import schedule as SCH

    b = pd.read_csv(target_csv)
    if "line" not in b.columns:
        raise ValueError(f"{target_csv} has no `line` column of Alpaca symbols: rebuild the target book (amendment A14)")
    col = "line"
    yahoo = dict(zip(b[col], b["ticker"], strict=True))
    if odd := sorted(f"{s}->{y}" for s, y in yahoo.items() if str(s).replace(".", "-") != str(y).replace(".", "-")):
        raise ValueError(f"target book maps Alpaca symbols to different Yahoo tickers (prices would come from another security): {odd[:10]}")
    px_y, px_date = latest_prices(sorted(set(b["ticker"])))
    prices = {t: px_y[y] for t, y in yahoo.items() if y in px_y}
    info = Path(target_csv).with_suffix(".json")
    meta = json.loads(info.read_text()) if info.exists() else {}
    short_fraction, lev = float(meta.get("short_fraction", 0.0)), float(meta.get("leverage", 1.0))
    weights = dict(zip(b[col], b["w"] / b["w"].sum() * lev, strict=True))          # A11 books: longs 1 + s, shorts -s, total 1; A12 scales both by k (total k)
    target = SCH.tranche_target_shares(weights, prices, account_value_usd, exposure)
    target = {t: (float(math.trunc(v)) if weights[t] < 0 else v) for t, v in target.items()}   # whole-share shorts, toward zero (A11)
    plan = make_plan(target, held or {}, prices, account_value_usd)
    if short_fraction:
        plan["short_fraction"] = short_fraction
    if lev != 1.0:
        plan["leverage"] = lev
    plan.update({"broker": "alpaca", "slot": str(slot), "tranche": SCH.tranche_of(slot), "k": SCH.K_TRANCHES, "exposure": exposure, "target_book": str(target_csv), "price_date": px_date,
                 "held": dict(held or {}), "held_source": "assumed empty (account not read)" if held is None else "given", "usd_total": round(sum(o["usd"] for o in plan["orders"]), 2)})
    return plan
