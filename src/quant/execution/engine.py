"""PAPER execution engine v1 (lab/paper/SPEC.md): the order path. Paper endpoints only: the Alpaca paper account (amendment A10, the broker in use) or the
Trading 212 practice account (kept, unused). The one module that can send an order (tests/contracts/test_broker_boundary.py).
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
from quant.execution import audit as A
from quant.execution import ledger as LG
from quant.portfolio import schedule as SCH
from quant.portfolio.broker import BROKER, MAX_GROSS, MAX_SHORT_FRAC, MAX_SHORT_LEG, SHORTS, usd_line
from quant.portfolio.plan import QTY_DECIMALS, ROOT, book_exposure, plan_from_target, plan_hash, write_plan
from quant.core.paths import REPO
from quant.execution.brokers.t212.api import DEMO, auth, positions

CONFIRM, KILL, LOCK = ROOT / "confirm", ROOT / "KILL", ROOT / "SEND.lock"
PREFLIGHT = [sys.executable, "-m", "quant.execution.preflight"], REPO / "lab/ready/results/preflight.json"
MAX_ORDER_FRAC = 0.10                                       # SPEC section 4: refuse any single order above 10% of the account
MAX_PLAN_AGE = dt.timedelta(days=4)                         # a plan made from Friday's close can be sent on the Tuesday slot, not a week later
SEND_BROKER = BROKER                                        # amendment A10: plans for any other broker are refused
NEW_YORK = ZoneInfo("America/New_York")
AUCTION_CLOSED = (dt.time(9, 28), dt.time(19, 0))            # amendment A13: Alpaca rejects opening-auction orders sent between these New York times
CLOCK: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731 - replaced in tests


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
    """The broker a plan was made for; a plan without the field predates amendment A10 and is a Trading 212 plan."""
    broker: str = plan.get("broker", "t212")
    return broker


def broker_positions(broker: str) -> dict[str, float]:
    return ALP.positions() if broker == "alpaca" else positions()


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
    """(order, shares opened short) for each order that takes the tranche's recorded holding below zero."""
    held = plan.get("held", {})
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


def check_gates(plan: dict[str, Any], preflight_go: bool, now: dt.datetime | None = None) -> None:
    """Raises Refused naming the first gate that fails, in the order of the module docstring."""
    h, orders, acct = plan["plan_hash"], plan["orders"], plan["account_value_usd"]
    if KILL.exists():
        raise Refused("kill switch present")
    if not preflight_go:
        raise Refused("preflight is not GO")
    if broken := A.verify()["broken"]:
        raise Refused(f"audit log chain broken at line(s) {broken}")
    if plan_hash(orders, acct) != h:
        raise Refused("plan changed since it was written")
    b = broker_of(plan)
    if b != SEND_BROKER:
        raise Refused(f"plan is for broker {b!r}; amendment A10 sends only to {SEND_BROKER!r}")
    past = history(h)
    made = [r for r in past if r["event"] == "plan"]
    if not made:
        raise Refused("no plan record for this hash in the audit log")
    if made[0].get("broker", "t212") != b:
        raise Refused("the plan's broker differs from the one recorded when it was made")
    if (now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(made[0]["utc"]) > MAX_PLAN_AGE:
        raise Refused(f"plan recorded {made[0]['utc']}, more than {MAX_PLAN_AGE.days} days ago: make a new plan at current prices")
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
    bad = [o["ticker"] for o in orders if not (math.isfinite(o["quantity"]) and o["quantity"] != 0 and o["side"] == ("BUY" if o["quantity"] > 0 else "SELL"))]
    if bad or len(set(tickers)) != len(tickers):
        raise Refused(f"malformed orders: {bad or 'a ticker appears twice'}")
    if big := [o["ticker"] for o in orders if o["usd"] > MAX_ORDER_FRAC * acct]:
        raise Refused(f"orders above {MAX_ORDER_FRAC:.0%} of the account: {big}")
    if other := [t for t in tickers if not usd_line(t, b)]:
        raise Refused(f"non-USD lines: {other}")
    if b == "alpaca":
        try:
            ok = alpaca_tradable()
        except FileNotFoundError as e:
            raise Refused(f"no Alpaca asset list to check the symbols against: {e}") from e
        if missing := [t for t in tickers if t not in ok]:
            raise Refused(f"symbols not active and tradable in the newest Alpaca asset list: {missing}")
    sf = plan.get("short_fraction", 0.0)
    if not (isinstance(sf, (int, float)) and math.isfinite(sf) and 0.0 <= sf <= MAX_SHORT_FRAC):
        raise Refused(f"short_fraction {sf!r} outside [0, {MAX_SHORT_FRAC}]")
    if sf > 0 and not (SHORTS and b == "alpaca"):
        raise Refused("this plan shorts, but short selling is enabled only on the Alpaca path (amendment A11)")
    lev = plan.get("leverage", 1.0)
    if not (isinstance(lev, (int, float)) and math.isfinite(lev) and lev >= 1.0 and lev * (1 + 2 * sf) <= MAX_GROSS + 1e-9):
        raise Refused(f"leverage {lev!r} outside [1, {MAX_GROSS} / (1 + 2 x short fraction)]")
    if lev > 1.0 and b != "alpaca":
        raise Refused("this plan is levered, but leverage is enabled only on the Alpaca margin account (amendment A12)")
    if (buys := sum(o["usd"] for o in orders if o["side"] == "BUY")) > acct * lev * (1 + sf):
        raise Refused(f"buys total {buys:,.2f} USD, more than the account's {acct:,.2f}" if not sf and lev == 1.0
                      else f"buys total {buys:,.2f} USD, more than (1 + {sf}) x the account" if lev == 1.0
                      else f"buys total {buys:,.2f} USD, more than {lev} x (1 + {sf}) x the account")
    if b == "alpaca" and (ss := short_sales(plan)):
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


def http_post(path: str, body: dict[str, Any], auth: str) -> dict[str, Any]:
    req = urllib.request.Request(DEMO + path, data=json.dumps(body).encode(), method="POST", headers={"Authorization": auth, "Content-Type": "application/json", "User-Agent": "quant-research-paper"})
    with urllib.request.urlopen(req, timeout=60) as r:
        answer: dict[str, Any] = json.loads(r.read())
        return answer


def qty_text(q: float) -> str:
    """|q| as Alpaca's decimal string at the plan's four decimals, without trailing zeros."""
    return f"{abs(q):.{QTY_DECIMALS}f}".rstrip("0").rstrip(".")


def alpaca_orders(o: dict[str, Any], h: str, i: int) -> list[dict[str, Any]]:
    """The broker orders for one plan line (amendment A13): its whole shares as a market-on-open order (time in force `opg`, filled in the opening
    auction), and any fractional remainder as a market day order (Alpaca accepts fractions only as day orders). Together they are exactly the line's quantity."""
    q = abs(o["quantity"])
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
    answers = []
    for body in alpaca_orders(o, h, i):
        try:
            answers.append(alpaca_post(body))
        except urllib.error.HTTPError as e:
            if body["time_in_force"] != "opg" or not 400 <= e.code < 500:
                raise
            day = {**body, "time_in_force": "day", "client_order_id": body["client_order_id"] + "d"}
            A.log("opg_refused_day_sent", plan_hash=h, order=o, status=e.code, reason=e.read().decode(errors="replace")[:200])
            answers.append(alpaca_post(day))
    return answers


def alpaca_post(body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(ALP.PAPER + "/v2/orders", data=json.dumps(body).encode(), method="POST",
                                 headers={**ALP.headers(), "Content-Type": "application/json", "User-Agent": "quant-research-paper"})
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
        if b == "alpaca" and (late := auction_window_problem(CLOCK())):
            raise Refused(late)
    except Refused as e:
        A.log("refused", plan_hash=h, reason=str(e))
        raise
    full = auth("FULL") if b == "t212" else None
    A.log("send_started", plan_hash=h, token=str(CONFIRM / f"{h}.token"), orders=len(plan["orders"]), held_before=before, broker=b)
    out: list[dict[str, Any]] = []
    for i, o in enumerate(plan["orders"]):
        if KILL.exists():
            A.log("halted_by_kill_switch", plan_hash=h, sent=len(out))
            break
        try:
            if b == "alpaca":
                answers = send_alpaca_line(o, cast(str, h), i)
                r: dict[str, Any] = answers[0] if len(answers) == 1 else {"orders": answers}
            else:
                r = http_post("/equity/orders/market", {"ticker": o["ticker"], "quantity": o["quantity"]}, cast(str, full))
            A.log("sent", plan_hash=h, order=o, response=r)
            out.append({"order": o, "response": r})
        except Exception as e:  # noqa: BLE001 - record and continue to the audit; no automatic retry
            A.log("error", plan_hash=h, order=o, error=repr(e)[:200])
            out.append({"order": o, "error": repr(e)[:200]})
        time.sleep(sleep)
    A.log("send_finished", plan_hash=h, accepted=sum("response" in x for x in out), errors=sum("error" in x for x in out), not_attempted=len(plan["orders"]) - len(out))
    return out


def reconcile(h: str, orders: list[dict[str, Any]], held_before: dict[str, float], held_after: dict[str, float], tol: float = 1e-3) -> list[dict[str, Any]]:
    """Differences between the positions read back and those expected: `held_before` plus `orders`."""
    exp = dict(held_before)
    for o in orders:
        exp[o["ticker"]] = round(exp.get(o["ticker"], 0.0) + o["quantity"], QTY_DECIMALS)
    diffs = []
    for t in sorted(set(exp) | set(held_after)):
        d = held_after.get(t, 0.0) - exp.get(t, 0.0)
        if abs(d) > tol:
            diffs.append({"ticker": t, "expected": exp.get(t, 0.0), "actual": held_after.get(t, 0.0), "diff": round(d, QTY_DECIMALS)})
    A.log("reconciled", plan_hash=h, differences=len(diffs))
    return diffs


def reconcile_sent(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconcile a sent plan from its audit record: the positions before the batch, the orders the broker accepted, and the positions read now.
    Orders that failed are not expected to have filled; they are counted as errors in the log, not as differences."""
    past = history(plan["plan_hash"])
    started = [r for r in past if r["event"] == "send_started"]
    if not started:
        raise Refused("this plan was never sent")
    accepted = [r["order"] for r in past if r["event"] == "sent"]
    diffs = reconcile(plan["plan_hash"], accepted, started[-1]["held_before"], broker_positions(broker_of(plan)))
    if not diffs:                                               # the tranche now holds what it held plus what the broker accepted: the start of its next plan
        new = dict(plan.get("held", {}))
        for o in accepted:
            new[o["ticker"]] = round(new.get(o["ticker"], 0.0) + o["quantity"], QTY_DECIMALS)
        LG.record_batch(LG.load(), plan["tranche"], new)
        A.log("ledger_updated", plan_hash=plan["plan_hash"], tranche=plan["tranche"], lines=sum(1 for q in new.values() if q))
    return diffs


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
    for c in ("send", "reconcile"):
        sub.add_parser(c).add_argument("plan")
    sub.add_parser("verify-audit")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        slot = dt.date.fromisoformat(a.slot)
        i = SCH.tranche_of(slot)
        if u := unsettled(i):
            print(f"REFUSED: tranche {i}'s last batch (plan {u[:12]}) has not reconciled with zero differences: reconcile it first")
            return 1
        exposure = a.exposure if a.exposure is not None else book_exposure(Path(a.target))
        plan = plan_from_target(Path(a.target), a.account_usd, exposure, slot, held=LG.tranche(LG.load(), i))
        plan.update({"held_source": f"ledger, tranche {i}", "exposure_source": "given" if a.exposure is not None else "target book overlay state"})
        f = write_plan(plan)
        print(json.dumps({k: v for k, v in plan.items() if k not in ("orders", "skipped")}, indent=1), "\n", f)
        return 0
    if a.cmd == "verify-audit":
        r = A.verify()
        print(json.dumps(r, indent=1))
        return 1 if r["broken"] else 0
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
