"""PAPER execution engine v1 (lab/paper/SPEC.md): the order path. One endpoint only: the Alpaca paper account (amendment A10; amendment A14 retired
the other broker path). The one module that can send an order (tests/contracts/test_broker_boundary.py).
What to trade is planned by quant.portfolio.plan; this module checks a plan, sends it and reconciles it.

    python -m quant.execution.engine plan   TARGET.csv   -> lab/paper/plans/<plan_hash>.json   (quant.portfolio.plan.plan_from_target; the account is not read)
    python -m quant.execution.engine send   PLAN.json    -> refuses unless every gate holds; sends nothing without the owner's token for THIS plan
    python -m quant.execution.engine reconcile PLAN.json -> positions read back against those before the batch plus the orders the broker accepted
    python -m quant.execution.engine verify-audit        -> checks the audit log's hash chain (exit 1 if broken)

Gates for `send` (all must hold; SPEC sections 4-5, amendments A5 and A6): kill-switch file absent; preflight GO from a run that completed; audit log chain intact; plan
hash unchanged; the plan was recorded by `plan` within the last MAX_PLAN_AGE and never sent before; it names its tranche, that tranche's previous batch reconciled with
zero differences, and the tranche's recorded holdings (lab/paper/ledger.json) are those the plan started from; owner token file `lab/paper/confirm/<plan_hash>.token`
contains that hash (the owner writes it, this code never does); every order well formed, a USD line and at most 10% of the account; buys together at most the account;
the plan is for the broker in use (quant.portfolio.broker.BROKER) and was recorded for that broker; for Alpaca, every symbol is tradable in the newest asset
list (quant.data.instruments.alpaca); and, last (amendment A7), rust_core's independent pre-trade check (`quant.core.kernels.pre_trade_check`, in-process through the C ABI) finds no reason, which needs the
Rust library built from the current sources (`tools/py -m quant.core.kernels build`): if it cannot run, the plan is refused. For Alpaca (amendment A13) the send is
also refused between 09:28 and 19:00 New York time on weekdays, when opening-auction orders are rejected: each line goes out as its whole shares market-on-open
(`opg`) and its fractional remainder as a market day order (`alpaca_orders`).
The positions are read before the first order. Every attempt, allowed or refused, is appended to the audit log (quant.execution.audit). `plan` starts from the tranche's
recorded holdings and the target book's overlay exposure; a reconciliation with zero differences records the tranche's new holdings in the ledger.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from quant.core import kernels as KN
from quant.execution.brokers.alpaca import api as ALP
from quant.execution.brokers.http import with_secrets
from quant.execution import audit as A
from quant.execution import ledger as LG
from quant.portfolio import schedule as SCH
from quant.portfolio.broker import BROKER, MAX_GROSS, MAX_SHORT_FRAC, MAX_SHORT_LEG, SHORTS, usd_line
from quant.portfolio import plan as P
from quant.portfolio.plan import QTY_DECIMALS, ROOT, book_exposure, plan_from_target, plan_hash, write_plan
from quant.core.paths import REPO

CONFIRM, KILL, LOCK = ROOT / "confirm", ROOT / "KILL", ROOT / "SEND.lock"
PREFLIGHT = [sys.executable, "-m", "quant.execution.preflight"], REPO / "lab/ready/results/preflight.json"
MAX_ORDER_FRAC = 0.10                                       # SPEC section 4: refuse any single order above 10% of the account
MAX_PLAN_AGE = dt.timedelta(days=4)                         # a plan made from Friday's close can be sent on the Tuesday slot, not a week later
SEND_BROKER = BROKER                                        # amendment A10: plans for any other broker are refused
QTY_TOLERANCE = 5e-5                                        # amendment A16: half the smallest quantity step (4 decimals): a fraction of a share is a difference
MAX_GROSS_EXCESS = 0.10                                     # amendment A17: account-wide gross after the batch at most MAX_GROSS x equity x 1.10 (prices move between batches)
SLOT_DAYS_BEFORE, SLOT_DAYS_AFTER = 1, 1                    # amendment A17: send on the evening before the Tuesday slot, on it, or the day after (New York dates)
MAX_ACCOUNT_EXCESS = 0.05                                   # amendment A15: a plan may size on at most 5% more than the account's equity at send time
NEW_YORK = ZoneInfo("America/New_York")
AUCTION_CLOSED = (dt.time(9, 28), dt.time(19, 0))            # amendment A13: Alpaca rejects opening-auction orders sent between these New York times
CLOCK: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731 - replaced in tests


class Halted(Exception):
    """The kill switch appeared while a line was being sent (amendment A17): nothing of the line went out after it. `answers` are the orders of the line already accepted."""

    def __init__(self, answers: list[dict[str, Any]] | None = None) -> None:
        super().__init__("kill switch present")
        self.answers = answers or []


class Refused(Exception):
    """A gate failed; nothing was sent."""


def read_token(path: Path) -> str:
    """The text of the owner's token file, whatever editor wrote it: UTF-8 with or without a byte-order mark, or UTF-16 (what `echo ... > file` writes in Windows
    PowerShell 5.1); surrounding whitespace removed. Bytes that are not text read as an empty token, which no plan matches."""
    raw = path.read_bytes()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        return raw.decode(enc).strip()
    except UnicodeDecodeError:
        return ""


def broker_of(plan: dict[str, Any]) -> str:
    """The broker a plan was made for; "" for a plan without the field (made before amendment A10), which the broker gate refuses."""
    broker: str = plan.get("broker", "")
    return broker


def broker_positions(broker: str) -> dict[str, float]:
    """The positions of the account the plan's broker names; only Alpaca exists (amendment A14)."""
    if broker != "alpaca":
        raise Refused(f"no account for broker {broker!r}: amendment A14 keeps only the Alpaca paper account")
    return ALP.positions()


def alpaca_tradable() -> set[str]:
    from quant.data.instruments import alpaca as AM

    return AM.tradable_symbols(AM.latest_assets())


def alpaca_shortable() -> set[str]:
    """Symbols the newest asset list marks active, tradable, shortable and easy to borrow (amendment A11)."""
    from quant.data.instruments import alpaca as AM

    a = AM.latest_assets()
    ok = (a["status"] == "active") & a["tradable"].astype(bool) & a["shortable"].astype(bool) & a["easy_to_borrow"].astype(bool)
    return set(a.loc[ok, "symbol"].astype(str))


def short_sales(plan: dict[str, Any]) -> list[tuple[dict[str, Any], float]]:
    """(order, shares opened short) for each order that takes the ACCOUNT's holding of the name below zero (amendment A17): the sum over all tranches of the
    ledger, not this tranche's holding alone, because a sale of a long in one tranche adds to the account's short when another tranche is short the same name."""
    held = LG.aggregate(LG.load())
    out = []
    for o in plan["orders"]:
        before, after = held.get(o["ticker"], 0.0), held.get(o["ticker"], 0.0) + o["quantity"]
        if o["quantity"] < 0 and after < -1e-9:
            out.append((o, max(0.0, -after) - max(0.0, -before)))
    return out


def history(h: str) -> list[dict[str, Any]]:
    return [r for r in A.records() if r.get("plan_hash") == h]


def tranche_of_plan(rec: dict[str, Any]) -> int | None:
    """The tranche of a `plan` audit record: recorded since amendment A6, derived from the slot before it, None for a plan without either."""
    if rec.get("tranche") is not None:
        tranche: int = rec["tranche"]
        return tranche
    return SCH.tranche_of(dt.date.fromisoformat(rec["slot"])) if rec.get("slot") else None


def unsettled(tranche: int) -> str | None:
    """Hash of the newest sent plan of `tranche` that has no reconciliation with zero differences (its holdings are not yet known); None if every sent batch settled."""
    recs = A.records()
    of = {r["plan_hash"]: tranche_of_plan(r) for r in recs if r["event"] == "plan"}
    sent = [r["plan_hash"] for r in recs if r["event"] == "send_started" and of.get(r["plan_hash"]) == tranche]
    if not sent or any(r["event"] == "reconciled" and r.get("plan_hash") == sent[-1] and r.get("differences") == 0 for r in recs):
        return None
    newest: str = sent[-1]
    return newest


def sealed_unchanged(plan: dict[str, Any], record: dict[str, Any]) -> None:
    """Refuse a plan whose file no longer matches the digest recorded when it was written (amendment A15). The plan hash, which the owner's token
    confirms and rust_core re-derives, covers the orders, the account value and the tranche (A32); the holdings, the leverage and every other field
    are held by this digest."""
    if "plan_sha256" not in record:
        raise Refused("the plan was recorded without a digest of its file (before amendment A15): make a new plan")
    if P.plan_seal(plan) != record["plan_sha256"]:
        raise Refused("the plan file changed since it was recorded (tranche, holdings or another field): make a new plan")


def account_equity() -> float:
    """The Alpaca paper account's equity in USD, read now."""
    return ALP.equity_usd(ALP.account())


def check_account_value(plan: dict[str, Any]) -> float:
    """Refuse a plan sized on more than MAX_ACCOUNT_EXCESS above the account's equity read now, or when the equity cannot be read (amendment A15)."""
    try:
        equity = account_equity()
    except Exception as e:  # noqa: BLE001 - the limits cannot be checked against the account, so nothing is sent
        raise Refused(f"account equity could not be read: {e!r}"[:200]) from e
    if plan["account_value_usd"] > equity * (1 + MAX_ACCOUNT_EXCESS):
        raise Refused(f"the plan sizes orders on {plan['account_value_usd']:,.2f} USD but the account's equity is {equity:,.2f} USD "
                      f"(more than {MAX_ACCOUNT_EXCESS:.0%} above it): make a new plan with --account-usd at most the equity")
    return equity


def check_slot(plan: dict[str, Any], now: dt.datetime) -> None:
    """Refuse a plan whose Tuesday slot is not within a day of the New York date of sending (amendment A17): a plan for a future Tuesday, or an old one, must not
    trade on another day's open."""
    try:
        slot = dt.date.fromisoformat(str(plan.get("slot")))
    except ValueError:
        raise Refused(f"plan names no valid slot date ({plan.get('slot')!r})") from None
    if slot.weekday() != 1:
        raise Refused(f"plan slot {slot} is not a Tuesday")
    today = now.astimezone(NEW_YORK).date()
    if not -SLOT_DAYS_AFTER <= (slot - today).days <= SLOT_DAYS_BEFORE:
        raise Refused(f"plan is for the slot of {slot} but it is {today} in New York: send from the evening before the slot to the day after it, else make a new plan")


def gross_after(plan: dict[str, Any], positions: dict[str, tuple[float, float | None]]) -> float:
    """Account-wide gross exposure in USD after the plan's orders fill at the plan's prices: every held name valued at the broker's market value, the names the plan
    trades at the plan's price times the quantity held afterwards. Refuses (Refused) when a name the plan does not trade has no market value."""
    px = {o["ticker"]: o["usd"] / abs(o["quantity"]) for o in plan["orders"]}
    total = 0.0
    for sym, (_qty, mv) in positions.items():
        if sym in px:
            continue
        if mv is None:
            raise Refused(f"position {sym} has no market value from the broker, so the account's gross exposure cannot be checked")
        total += abs(mv)
    for o in plan["orders"]:
        total += abs((positions.get(o["ticker"], (0.0, None))[0] + o["quantity"]) * px[o["ticker"]])
    return total


def catch_up_siblings(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Amendment A30b: on a catch-up slot (schedule.CATCH_UP), the other plans of the same slot that were sent and have not settled (their orders are
    still pending at the broker). Empty on any other slot."""
    try:
        slot = dt.date.fromisoformat(str(plan.get("slot")))
    except ValueError:
        return []
    if slot not in SCH.CATCH_UP:
        return []
    recs = A.records()
    slot_of = {r["plan_hash"]: r.get("slot") for r in recs if r["event"] == "plan"}
    settled = {r["plan_hash"] for r in recs if r["event"] == "reconciled" and r.get("differences") == 0}
    sent = {r["plan_hash"] for r in recs if r["event"] == "send_started"}
    out = []
    for h in sorted(sent - settled - {plan.get("plan_hash")}):
        if slot_of.get(h) == str(slot) and (f := P.PLANS / f"{h}.json").exists():
            out.append(json.loads(f.read_text()))
    return out


def merged_orders(plans: list[dict[str, Any]]) -> dict[str, Any]:
    """One plan-shaped dict whose orders are the plans' orders summed by ticker (price per share from the first plan that trades it): the account after
    all of them fill, for the gross check (A30b)."""
    qty: dict[str, float] = {}
    px: dict[str, float] = {}
    for pl in plans:
        for o in pl["orders"]:
            qty[o["ticker"]] = qty.get(o["ticker"], 0.0) + o["quantity"]
            px.setdefault(o["ticker"], o["usd"] / abs(o["quantity"]))
    return {"orders": [{"ticker": t, "quantity": q, "usd": abs(q) * px[t]} for t, q in qty.items() if q]}


def check_gross(plan: dict[str, Any], equity: float) -> None:
    """Refuse a batch that would leave the whole account (all tranches, not just this plan) above MAX_GROSS x equity x (1 + MAX_GROSS_EXCESS) (amendment A17).
    On a catch-up slot the pending orders of the slot's other sent plans count as filled (A30b)."""
    try:
        positions = ALP.position_values()
    except Exception as e:  # noqa: BLE001 - the exposure cannot be checked, so nothing is sent
        raise Refused(f"positions could not be read: {e!r}"[:200]) from e
    combined = merged_orders([plan, *catch_up_siblings(plan)])
    gross, limit = gross_after(combined, positions), MAX_GROSS * equity * (1 + MAX_GROSS_EXCESS)
    if gross > limit:
        raise Refused(f"after this batch the account's gross exposure would be {gross:,.2f} USD, above {limit:,.2f} ({MAX_GROSS} x equity {equity:,.2f} x {1 + MAX_GROSS_EXCESS:g})")


def ledger_vs_positions(positions: dict[str, float]) -> str:
    """How the account's positions differ from the sum of the tranche ledger (amendment A16), or "" when they agree. A plan sells what the ledger says is
    held: if the account holds less (or more) because of a trade outside the engine, a sale can open a short position the short-sale gates never saw."""
    agg = LG.aggregate(LG.load())
    diffs = {t: round(positions.get(t, 0.0) - agg.get(t, 0.0), QTY_DECIMALS) for t in sorted(set(positions) | set(agg))}
    off = {t: d for t, d in diffs.items() if abs(d) > QTY_TOLERANCE}
    return "; ".join(f"{t}: account {positions.get(t, 0.0):g}, ledger {agg.get(t, 0.0):g}" for t in list(off)[:8]) + (" ..." if len(off) > 8 else "")


def check_decision_month(plan: dict[str, Any], today: dt.date) -> None:
    """Refuse a plan whose target book is stale (amendment A16): preflight's freshness rule judges the newest score file, but a plan can be made from any
    book, so the rule is applied to the decision month of the book the plan was made from."""
    from quant.execution import preflight as PF

    dm = plan.get("decision_month")
    if not (isinstance(dm, str) and len(dm) == 7 and dm[4] == "-" and dm[:4].isdigit() and dm[5:].isdigit() and 1 <= int(dm[5:]) <= 12):
        raise Refused(f"the plan names no valid decision month ({dm!r}): make a new plan from a target book that records one")
    age = PF.months_between(f"{dm}-01", today)
    if age < 0 or age > PF.MAX_SCORE_AGE_MONTHS + 1.0:
        raise Refused(f"the plan's target book is for decision month {dm} ({age:.1f} months old from its start; a plan needs the current or previous month): "
                      "make a new plan from the newest book")


def check_gates(plan: dict[str, Any], preflight_go: bool, now: dt.datetime | None = None) -> None:
    """Raises Refused naming the first gate that fails, in the order of the module docstring."""
    h, orders, acct = plan["plan_hash"], plan["orders"], plan["account_value_usd"]
    if KILL.exists():
        raise Refused("kill switch present")
    if not preflight_go:
        raise Refused("preflight is not GO")
    chain = A.verify()
    if chain["broken"]:
        raise Refused(f"audit log chain broken at line(s) {chain['broken']}")
    if chain["anchor_problem"]:
        raise Refused(f"audit log: {chain['anchor_problem']}")
    if plan_hash(orders, acct, plan.get("tranche")) != h:
        raise Refused("plan changed since it was written")
    b = broker_of(plan)
    if b != SEND_BROKER:
        raise Refused(f"plan is for broker {b!r}; amendment A10 sends only to {SEND_BROKER!r}")
    past = history(h)
    made = [r for r in past if r["event"] == "plan"]
    if not made:
        raise Refused("no plan record for this hash in the audit log")
    if made[0].get("broker", "") != b:
        raise Refused("the plan's broker differs from the one recorded when it was made")
    sealed_unchanged(plan, made[0])
    check_decision_month(plan, (now or dt.datetime.now(dt.timezone.utc)).date())
    if unsold := [s["ticker"] for s in plan.get("skipped", []) if s.get("reason") == "no_price" and s.get("ticker") in plan.get("held", {})]:
        raise Refused(f"positions the plan could not price, so could not trade: {unsold}: find their prices or close them by hand, then make a new plan")
    if (now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(made[0]["utc"]) > MAX_PLAN_AGE:
        raise Refused(f"plan recorded {made[0]['utc']}, more than {MAX_PLAN_AGE.days} days ago: make a new plan at current prices")
    check_slot(plan, CLOCK())
    if any(r["event"] in ("send_started", "sent") for r in past):
        raise Refused("this plan was already sent: make a new plan from the positions read back")
    if (i := plan.get("tranche")) is None:
        raise Refused("the plan names no tranche")
    if u := unsettled(i):
        raise Refused(f"tranche {i}'s last batch (plan {u[:12]}) has not reconciled with zero differences: reconcile it first")
    if LG.tranche(LG.load(), i) != plan.get("held", {}):
        raise Refused(f"tranche {i}'s recorded holdings changed since the plan was made: make a new plan")
    token = CONFIRM / f"{h}.token"
    if not token.exists() or read_token(token) != h:
        raise Refused("no owner confirmation for this plan")
    tickers = [o["ticker"] for o in orders]
    bad = [o["ticker"] for o in orders if not (math.isfinite(o["quantity"]) and o["quantity"] != 0 and o["side"] == ("BUY" if o["quantity"] > 0 else "SELL")
                                                and isinstance(o["usd"], (int, float)) and math.isfinite(o["usd"]) and o["usd"] > 0)]
    if bad or len(set(tickers)) != len(tickers):
        raise Refused(f"malformed orders: {bad or 'a ticker appears twice'}")
    if big := [o["ticker"] for o in orders if o["usd"] > MAX_ORDER_FRAC * acct]:
        raise Refused(f"orders above {MAX_ORDER_FRAC:.0%} of the account: {big}")
    if other := [t for t in tickers if not usd_line(t)]:
        raise Refused(f"not Alpaca US symbols: {other}")
    try:
        ok = alpaca_tradable()
    except FileNotFoundError as e:
        raise Refused(f"no Alpaca asset list to check the symbols against: {e}") from e
    if missing := [t for t in tickers if t not in ok]:
        raise Refused(f"symbols not active and tradable in the newest Alpaca asset list: {missing}")
    sf = plan.get("short_fraction", 0.0)
    if not (isinstance(sf, (int, float)) and math.isfinite(sf) and 0.0 <= sf <= MAX_SHORT_FRAC):
        raise Refused(f"short_fraction {sf!r} outside [0, {MAX_SHORT_FRAC}]")
    if sf > 0 and not SHORTS:
        raise Refused("this plan shorts, but short selling is not enabled (quant.portfolio.model, amendment A11)")
    lev = plan.get("leverage", 1.0)
    if not (isinstance(lev, (int, float)) and math.isfinite(lev) and lev >= 1.0 and lev * (1 + 2 * sf) <= MAX_GROSS + 1e-9):
        raise Refused(f"leverage {lev!r} outside [1, {MAX_GROSS} / (1 + 2 x short fraction)]")
    if (buys := sum(o["usd"] for o in orders if o["side"] == "BUY")) > acct * lev * (1 + sf):
        raise Refused(f"buys total {buys:,.2f} USD, more than the account's {acct:,.2f}" if not sf and lev == 1.0
                      else f"buys total {buys:,.2f} USD, more than (1 + {sf}) x the account" if lev == 1.0
                      else f"buys total {buys:,.2f} USD, more than {lev} x (1 + {sf}) x the account")
    check_gross(plan, check_account_value(plan))
    if ss := short_sales(plan):
        names = [o["ticker"] for o, _ in ss]
        if sf <= 0:
            raise Refused(f"short sales without a declared short fraction: {names}")
        if frac := [o["ticker"] for o, _ in ss if o["quantity"] != int(o["quantity"])]:
            raise Refused(f"short sales that are not whole shares: {frac}")
        try:
            can = alpaca_shortable()
        except FileNotFoundError as e:
            raise Refused(f"no Alpaca asset list to check short sales against: {e}") from e
        if htb := [t for t in names if t not in can]:
            raise Refused(f"short sales of names not shortable and easy to borrow: {htb}")
        cap = MAX_SHORT_LEG * acct / SCH.K_TRANCHES * 1.05
        if (opened := sum(n * o["usd"] / abs(o["quantity"]) for o, n in ss)) > cap:
            raise Refused(f"short sales total {opened:,.2f} USD, more than {cap:,.2f}")
    try:
        rust = KN.pre_trade_check(json.dumps(plan), KILL.exists(), preflight_go, read_token(token))
    except KN.PreTradeCheckUnavailable as e:
        raise Refused(f"Rust pre-trade check could not run: {e}") from e
    if rust:
        raise Refused(f"Rust pre-trade check: {'; '.join(rust)}")


def qty_text(q: float) -> str:
    """|q| as Alpaca's decimal string at the plan's four decimals, without trailing zeros."""
    return f"{abs(q):.{QTY_DECIMALS}f}".rstrip("0").rstrip(".")


DAY_ORDERS = True                                           # amendment A31: one market day order per line; the paper account expired most opening-auction orders


def alpaca_orders(o: dict[str, Any], h: str, i: int) -> list[dict[str, Any]]:
    """The broker orders for one plan line. Since amendment A31: one market day order for the whole quantity (fractions allowed on day orders).
    Under A13 (DAY_ORDERS False): the whole shares as a market-on-open order (time in force `opg`, filled in the opening auction) and any fractional
    remainder as a market day order. Either way they are exactly the line's quantity."""
    q = abs(o["quantity"])
    if DAY_ORDERS:
        return [{"symbol": o["ticker"], "qty": qty_text(q), "side": "buy" if o["quantity"] > 0 else "sell", "type": "market", "time_in_force": "day",
                 "client_order_id": f"{h[:24]}-{i}"}]
    whole = math.floor(q + 10 ** -(QTY_DECIMALS + 2))
    frac = round(q - whole, QTY_DECIMALS)
    side = "buy" if o["quantity"] > 0 else "sell"
    out = []
    if whole >= 1:
        out.append({"symbol": o["ticker"], "qty": str(whole), "side": side, "type": "market", "time_in_force": "opg", "client_order_id": f"{h[:24]}-{i}"})
    if frac > 0:
        out.append({"symbol": o["ticker"], "qty": qty_text(frac), "side": side, "type": "market", "time_in_force": "day", "client_order_id": f"{h[:24]}-{i}f"})
    return out


def auction_window_problem(now: dt.datetime) -> str | None:
    """Why opening-auction orders cannot be sent at `now` (amendment A13), or None: Alpaca rejects them from 09:28 to 19:00 New York time on weekdays."""
    ny = now.astimezone(NEW_YORK)
    lo, hi = AUCTION_CLOSED
    if ny.weekday() < 5 and lo <= ny.time() < hi:
        return (f"it is {ny:%H:%M} in New York: Alpaca rejects opening-auction orders sent between {lo:%H:%M} and {hi:%H:%M}; "
                "send after 19:00 New York time (00:00 UK in winter) for the next open")
    return None


def send_alpaca_line(o: dict[str, Any], h: str, i: int) -> list[dict[str, Any]]:
    """Post one plan line's broker orders; an opening-auction order Alpaca refuses (an HTTP 4xx answer) is sent once as a market day order instead,
    and that fallback is logged. Returns the broker's answers. Any other failure raises (the caller records it; no retry)."""
    answers: list[dict[str, Any]] = []
    for body in alpaca_orders(o, h, i):
        if KILL.exists():                                                             # amendment A17: checked before every post, not only before every line
            raise Halted(answers)
        try:
            answers.append(alpaca_post(body))
        except urllib.error.HTTPError as e:
            if body["time_in_force"] != "opg" or not 400 <= e.code < 500:
                raise
            if KILL.exists():
                raise Halted(answers) from e
            day = {**body, "time_in_force": "day", "client_order_id": body["client_order_id"] + "d"}
            A.log("opg_refused_day_sent", plan_hash=h, order=o, status=e.code, reason=e.read().decode(errors="replace")[:200])
            answers.append(alpaca_post(day))
    return answers


def alpaca_post(body: dict[str, Any]) -> dict[str, Any]:
    req = with_secrets(urllib.request.Request(ALP.PAPER + "/v2/orders", data=json.dumps(body).encode(), method="POST",
                                              headers={"Content-Type": "application/json", "User-Agent": "quant-research-paper"}), ALP.headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        answer: dict[str, Any] = json.loads(r.read())
        return answer


def send(plan: dict[str, Any], preflight_go: bool, sleep: float = 1.2) -> list[dict[str, Any]]:
    """Send a confirmed plan to the demo endpoint, one market order at a time, with no automatic retry. The positions before the batch are recorded for `reconcile`.
    One send at a time: the lock file is created exclusively, so a second process cannot pass the never-sent gate while the first is still sending."""
    try:
        lock = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        reason = f"another send is running, or one stopped without finishing: read the audit log, then delete {LOCK.name}"
        A.log("refused", plan_hash=plan.get("plan_hash"), reason=reason)
        raise Refused(reason) from None
    try:
        return _send(plan, preflight_go, sleep)
    finally:
        os.close(lock)
        LOCK.unlink()


def _send(plan: dict[str, Any], preflight_go: bool, sleep: float) -> list[dict[str, Any]]:
    h = plan.get("plan_hash")
    try:
        check_gates(plan, preflight_go)
        b = broker_of(plan)
        try:
            before = broker_positions(b)
        except Exception as e:  # noqa: BLE001 - without the positions before the batch it cannot be reconciled, so it is not sent
            raise Refused(f"positions could not be read: {e!r}"[:200]) from e
        if not DAY_ORDERS and (late := auction_window_problem(CLOCK())):                 # A31: day orders may be sent while the market is open
            raise Refused(late)
        ledger_differs = ledger_vs_positions(before)
        if ledger_differs:
            raise Refused(f"the account's positions are not what the tranche ledger holds ({ledger_differs}): find the trade outside the engine, "
                          "then settle the ledger, before any new batch")
        sibling = tuple(pl["plan_hash"][:24] for pl in catch_up_siblings(plan))
        if others := [o for o in foreign_open_orders() if not (sibling and o.startswith(sibling))]:      # A30b: a catch-up sibling's pending orders are expected
            raise Refused(f"{len(others)} order(s) are open at the broker ({', '.join(others[:5])}): a fill after the checks would change holdings and exposure; "
                          "cancel or wait for them, then send")
    except Refused as e:
        A.log("refused", plan_hash=h, reason=str(e))
        raise
    A.log("send_started", plan_hash=h, token=str(CONFIRM / f"{h}.token"), orders=len(plan["orders"]), held_before=before, broker=b)
    out: list[dict[str, Any]] = []
    for i, o in enumerate(plan["orders"]):
        if KILL.exists():
            A.log("halted_by_kill_switch", plan_hash=h, sent=len(out))
            break
        try:
            answers = send_alpaca_line(o, cast(str, h), i)
            r: dict[str, Any] = answers[0] if len(answers) == 1 else {"orders": answers}
            A.log("sent", plan_hash=h, order=o, response=r)
            out.append({"order": o, "response": r})
        except Halted as k:
            if k.answers:                                                             # part of the line went out: it is recorded as sent, and reconciliation will show the rest as a difference
                A.log("sent", plan_hash=h, order=o, response={"orders": k.answers, "partial": True})
                out.append({"order": o, "response": {"orders": k.answers, "partial": True}})
            A.log("halted_by_kill_switch", plan_hash=h, sent=len(out))
            break
        except Exception as e:  # noqa: BLE001 - record and continue to the audit; no automatic retry
            A.log("error", plan_hash=h, order=o, error=repr(e)[:200])
            out.append({"order": o, "error": repr(e)[:200]})
        time.sleep(sleep)
    A.log("send_finished", plan_hash=h, accepted=sum("response" in x for x in out), errors=sum("error" in x for x in out), not_attempted=len(plan["orders"]) - len(out))
    return out


def reconcile(orders: list[dict[str, Any]], held_before: dict[str, float], held_after: dict[str, float], tol: float = QTY_TOLERANCE) -> list[dict[str, Any]]:
    """Differences between the positions read back and those expected: `held_before` plus `orders`. Writes nothing: the settled record belongs after the
    ledger (`reconcile_sent`)."""
    exp = dict(held_before)
    for o in orders:
        exp[o["ticker"]] = round(exp.get(o["ticker"], 0.0) + o["quantity"], QTY_DECIMALS)
    diffs = []
    for t in sorted(set(exp) | set(held_after)):
        d = held_after.get(t, 0.0) - exp.get(t, 0.0)
        if abs(d) > tol:
            diffs.append({"ticker": t, "expected": exp.get(t, 0.0), "actual": held_after.get(t, 0.0), "diff": round(d, QTY_DECIMALS)})
    return diffs


def foreign_open_orders() -> list[str]:
    """Ids of every order open at Alpaca (amendment A21), checked before a send: this engine sends one batch at a time and reconciles before the next, so
    any open order is one placed outside it, or an earlier batch not yet settled; either can fill after the position and exposure checks."""
    open_orders: list[dict[str, Any]] = ALP.get("/v2/orders?status=open&limit=500")
    return sorted(str(o.get("client_order_id") or o.get("id")) for o in open_orders)


def open_plan_orders(plan: dict[str, Any]) -> list[str]:
    """Client order ids of this plan's orders still open at Alpaca (amendment A16): an order accepted but not yet filled is not a difference yet, so a
    reconciliation made while one is open could settle the tranche and let the next plan trade the same names again."""
    prefix = plan["plan_hash"][:24]
    open_orders: list[dict[str, Any]] = ALP.get("/v2/orders?status=open&limit=500")
    return sorted(str(o.get("client_order_id")) for o in open_orders if str(o.get("client_order_id", "")).startswith(prefix))


OPEN_STATUSES = frozenset({"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "held", "pending_replace", "calculated"})


def alpaca_lookup(client_order_id: str) -> dict[str, Any] | None:
    """The Alpaca order with this client order id, or None if Alpaca has none (HTTP 404)."""
    try:
        found: dict[str, Any] = ALP.get(f"/v2/orders:by_client_order_id?client_order_id={client_order_id}")
        return found
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def recovered_fills(plan: dict[str, Any], past: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plan lines whose send raised (a timeout, a lost response) looked up at Alpaca by their client order ids (amendment A15). One still open there
    refuses the reconciliation (it may yet fill); one that filled counts as an accepted order of its filled quantity, and is logged."""
    h, out = plan["plan_hash"], []
    for r in (r for r in past if r["event"] == "error"):
        o = r["order"]
        i = plan["orders"].index(o)
        filled = 0.0
        for cid in (f"{h[:24]}-{i}", f"{h[:24]}-{i}f", f"{h[:24]}-{i}d"):
            got = alpaca_lookup(cid)
            if got is None:
                continue
            if got.get("status") in OPEN_STATUSES:
                raise Refused(f"order {cid} ({o['ticker']}) whose send failed is still open at Alpaca ({got.get('status')}): reconcile after it ends")
            filled += float(got.get("filled_qty") or 0)
        if filled:
            q = round(math.copysign(filled, o["quantity"]), QTY_DECIMALS)
            A.log("errored_order_found_filled", plan_hash=h, order=o, filled_quantity=q)
            out.append({**o, "quantity": q})
    return out


def reconcile_sent(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconcile a sent plan from its audit record: the positions before the batch, the orders the broker accepted, and the positions read now.
    Orders that failed are not expected to have filled; they are counted as errors in the log, not as differences."""
    past = history(plan["plan_hash"])
    started = [r for r in past if r["event"] == "send_started"]
    if not started:
        raise Refused("this plan was never sent")
    if LOCK.exists():
        raise Refused(f"a send is running, or one stopped without finishing ({LOCK.name} exists): reconcile after it ends")
    if not any(r["event"] == "send_finished" for r in past):
        raise Refused("the send of this plan has no send_finished record: it stopped part way; read the audit log before reconciling")
    made = [r for r in past if r["event"] == "plan"]
    if not made:
        raise Refused("no plan record for this hash in the audit log")
    sealed_unchanged(plan, made[0])
    accepted = [r["order"] for r in past if r["event"] == "sent"] + recovered_fills(plan, past)
    still_open = open_plan_orders(plan)
    if still_open:
        raise Refused(f"orders of this plan are still open at Alpaca ({', '.join(still_open[:6])}): reconcile after they fill or are cancelled")
    diffs = reconcile(accepted, started[-1]["held_before"], broker_positions(broker_of(plan)))
    if not diffs:                                               # the tranche now holds what it held plus what the broker accepted: the start of its next plan
        new = dict(plan.get("held", {}))
        for o in accepted:
            new[o["ticker"]] = round(new.get(o["ticker"], 0.0) + o["quantity"], QTY_DECIMALS)
        LG.record_batch(LG.load(), plan["tranche"], new)          # the ledger first: a settled record is never written before the holdings it settles
        A.log("ledger_updated", plan_hash=plan["plan_hash"], tranche=plan["tranche"], lines=sum(1 for q in new.values() if q))
    A.log("reconciled", plan_hash=plan["plan_hash"], differences=len(diffs))
    return diffs


def plan_fills(plan: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, float]:
    """Signed filled quantity by symbol of the broker orders whose client order id carries this plan's prefix (amendment A31)."""
    out: dict[str, float] = {}
    for o in orders:
        if str(o.get("client_order_id", "")).startswith(plan["plan_hash"][:24]) and float(o.get("filled_qty") or 0):
            q = float(o["filled_qty"]) * (1 if o["side"] == "buy" else -1)
            out[o["symbol"]] = round(out.get(o["symbol"], 0.0) + q, QTY_DECIMALS)
    return out


def settle_from_fills(plans: list[dict[str, Any]], orders: list[dict[str, Any]], positions: dict[str, float]) -> dict[int, dict[str, float]]:
    """Amendment A31: each sent plan's tranche holds what it held before its batch plus its own orders' filled quantities. Returned only when the whole
    ledger after that (these tranches settled, every other tranche as recorded) equals the broker's positions symbol by symbol; otherwise Refused."""
    ledger = LG.load()
    new = {int(pl["tranche"]): dict(LG.tranche(ledger, int(pl["tranche"]))) for pl in plans}
    for pl in plans:
        if LG.tranche(ledger, int(pl["tranche"])) != pl.get("held", {}):
            raise Refused(f"tranche {pl['tranche']}'s ledger is not what plan {pl['plan_hash'][:12]} started from")
        for sym, q in plan_fills(pl, orders).items():
            new[int(pl["tranche"])][sym] = round(new[int(pl["tranche"])].get(sym, 0.0) + q, QTY_DECIMALS)
    total: dict[str, float] = {}
    for i in range(SCH.K_TRANCHES):
        for sym, q in (new[i] if i in new else LG.tranche(ledger, i)).items():
            total[sym] = total.get(sym, 0.0) + q
    syms = {k for k, v in total.items() if abs(v) > 1e-6} | {k for k, v in positions.items() if abs(v) > 1e-6}
    if bad := sorted(k for k in syms if abs(total.get(k, 0.0) - positions.get(k, 0.0)) > 10 ** -QTY_DECIMALS):
        raise Refused(f"the tranches settled from fills do not add up to the broker's positions: {bad[:8]}")
    return new


def broker_orders_since(utc: str) -> list[dict[str, Any]]:
    """Every Alpaca order submitted after `utc` (paged, 500 at a time)."""
    out: dict[str, dict[str, Any]] = {}
    after = utc.replace("+00:00", "Z")                         # a "+" in the query string reads as a space: Alpaca answers 422
    while True:
        page: list[dict[str, Any]] = ALP.get(f"/v2/orders?status=all&after={after}&limit=500&direction=asc")
        out.update({o["id"]: o for o in page})
        if len(page) < 500:
            return list(out.values())
        after = page[-1]["submitted_at"]


def settle_fills_cmd(paths: list[str]) -> int:
    plans = [json.loads(Path(f).read_text()) for f in paths]
    for pl in plans:
        past = history(pl["plan_hash"])
        if not any(r["event"] == "send_finished" for r in past):
            raise Refused(f"plan {pl['plan_hash'][:12]} has no finished send")
        if not (made := [r for r in past if r["event"] == "plan"]):
            raise Refused(f"no plan record for plan {pl['plan_hash'][:12]} in the audit log")
        sealed_unchanged(pl, made[0])                                     # as reconcile_sent: the tranche and holdings settled are those recorded
        if open_plan_orders(pl):
            raise Refused(f"orders of plan {pl['plan_hash'][:12]} are still open: settle after they fill or expire")
    first = min(r["utc"] for pl in plans for r in history(pl["plan_hash"]) if r["event"] == "send_started")
    new = settle_from_fills(plans, broker_orders_since(first), broker_positions(BROKER))
    for pl in plans:
        i = int(pl["tranche"])
        LG.record_batch(LG.load(), i, new[i])                 # the ledger first, as in reconcile_sent
        A.log("ledger_updated", plan_hash=pl["plan_hash"], tranche=i, lines=sum(1 for q in new[i].values() if q), basis="broker fills (A31)")
        A.log("reconciled", plan_hash=pl["plan_hash"], differences=0, basis="broker fills (A31)")
    print(json.dumps({str(i): len(h) for i, h in new.items()}))
    return 0


def preflight_go(cmd: list[str] = PREFLIGHT[0], result: Path = PREFLIGHT[1]) -> bool:
    """Run the read-only preflight now. GO only if this run exited 0 (it exits 1 on NO-GO and on a crash) and its result file says GO for today."""
    rc = subprocess.run(cmd, cwd=REPO, check=False, capture_output=True).returncode
    r = json.loads(result.read_text()) if result.exists() else {}
    return rc == 0 and bool(r.get("go")) and r.get("run_date") == dt.date.today().isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quant.execution.engine", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("target")
    p.add_argument("--account-usd", type=float, required=True)
    p.add_argument("--exposure", type=float, default=None, help="overlay exposure; default: the one the target book recorded")
    p.add_argument("--slot", required=True, help="the Tuesday, YYYY-MM-DD")
    p.add_argument("--tranche", type=int, default=None, help="another tranche than the slot's own; only where schedule.CATCH_UP lists it (amendment A30)")
    for c in ("send", "reconcile"):
        sub.add_parser(c).add_argument("plan")
    sub.add_parser("settle-fills", help="amendment A31: settle sent plans' tranches from their orders' fills").add_argument("plans", nargs="+")
    sub.add_parser("verify-audit")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        slot = dt.date.fromisoformat(a.slot)
        i = SCH.tranche_of(slot) if a.tranche is None else a.tranche
        if not SCH.plannable(slot, i):
            print(f"REFUSED: tranche {i} is not planned on the slot of {slot} (its tranche is {SCH.tranche_of(slot)}; no amendment lists a catch-up)")
            return 1
        if u := unsettled(i):
            print(f"REFUSED: tranche {i}'s last batch (plan {u[:12]}) has not reconciled with zero differences: reconcile it first")
            return 1
        exposure = a.exposure if a.exposure is not None else book_exposure(Path(a.target))
        plan = plan_from_target(Path(a.target), a.account_usd, exposure, slot, held=LG.tranche(LG.load(), i), tranche=i)
        plan.update({"held_source": f"ledger, tranche {i}", "exposure_source": "given" if a.exposure is not None else "target book overlay state"})
        f = write_plan(plan)
        print(json.dumps({k: v for k, v in plan.items() if k not in ("orders", "skipped")}, indent=1), "\n", f)
        return 0
    if a.cmd == "settle-fills":
        try:
            return settle_fills_cmd(a.plans)
        except Refused as e:
            A.log("refused", reason=f"settle-fills: {e}")
            print(f"REFUSED: {e}")
            return 1
    if a.cmd == "verify-audit":
        r = A.verify()
        print(json.dumps(r, indent=1))
        return 1 if r["broken"] or r["anchor_problem"] else 0
    plan = json.loads(Path(a.plan).read_text())
    try:
        out = send(plan, preflight_go(*PREFLIGHT)) if a.cmd == "send" else reconcile_sent(plan)
    except Refused as e:                                        # already in the audit log (send records every refusal); one line for the operator
        print(f"REFUSED: {e}")
        return 1
    print(json.dumps(out, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
