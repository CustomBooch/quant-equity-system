"""Read-only typed views of the PAPER engine's records, for reconciliation and fill analysis (lab/t212/INTEGRATION_SPEC.md): the audit log (quant.execution.audit), the
plans (quant.portfolio.plan.PLANS) and the tranche ledger (quant.execution.ledger). Nothing here writes: the engine is the only writer of these records, and this module only
calls their readers.

    read_engine_state()   every batch the audit log records (its plan, its start and the positions read before it, each order accepted or failed, the orders
                          not attempted, whether it settled) and the ledger; refuses an audit log whose hash chain is broken, as the engine refuses to send
    load_plan(plan_hash)  a stored plan; refuses one whose orders and account value no longer hash to its name
    expected_holdings()   what the engine's records say the account holds: the ledger's tranches plus the orders the broker accepted in batches not yet settled

Records are read strictly (quant.execution.brokers.t212.schema): an event the engine does not write, a record with a field missing, of the wrong type or not written by the engine,
or records that contradict the engine's rules (an order sent for a batch never started), raise EngineRecordError naming the audit line or the plan.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from quant.execution import audit as A
from quant.execution import ledger as LG
from quant.portfolio import plan as P
from quant.execution.engine import tranche_of_plan
from quant.execution.brokers.t212.schema import Fields, SchemaError, as_number_map, loads, parse_object

EVENTS = frozenset({"plan", "refused", "send_started", "sent", "error", "halted_by_kill_switch", "send_finished", "reconciled", "ledger_updated"})
QTY_DECIMALS = P.QTY_DECIMALS                                # share quantities are rounded as the engine rounds them


class EngineRecordError(Exception):
    """An engine record that cannot be read as the engine writes it, or records that contradict the engine's rules."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedOrder:
    """One order of a plan: signed share quantity (sells negative) and its notional at the plan's price, in USD rounded to the cent (quant.portfolio.plan)."""
    ticker: str
    quantity_shares: float
    notional_usd: float
    side: str

    @property
    def plan_price_usd(self) -> float:
        """The plan's price per share, recovered from the rounded notional."""
        return self.notional_usd / abs(self.quantity_shares)

    @property
    def plan_price_bound_bp(self) -> float:
        """Largest error of plan_price_usd caused by the cent rounding of the notional, in basis points of the price."""
        return 0.005 / self.notional_usd * 1e4


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    """A stored plan (lab/paper/plans/<plan_hash>.json): its tranche, the account value it was sized on, its orders and the tranche holdings it started from
    (empty for a plan made before amendment A6, which assumed an empty tranche)."""
    plan_hash: str
    tranche: int | None
    slot: dt.date | None
    account_value_usd: float
    orders: tuple[PlannedOrder, ...]
    held_shares: Mapping[str, float]
    price_date: dt.date | None
    exposure: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SentOrder:
    """An order the broker accepted (a `sent` audit record) with the answer it gave, as the engine logged it (decoded JSON, read by quant.execution.brokers.t212.records later)."""
    plan_hash: str
    order: PlannedOrder
    sent_utc: dt.datetime
    audit_line: int
    response: object


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedOrder:
    """An order whose submission failed (an `error` audit record): the engine does not expect it to have traded."""
    plan_hash: str
    order: PlannedOrder
    failed_utc: dt.datetime
    audit_line: int
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Batch:
    """One sent plan: when it was planned and started, the account's positions read before its first order, what became of each order, and whether a
    reconciliation with zero differences settled it (the engine's rule for a tranche's holdings being known)."""
    plan_hash: str
    tranche: int | None
    planned_utc: dt.datetime
    started_utc: dt.datetime
    started_line: int
    planned_orders: int
    held_before: Mapping[str, float]
    sent: tuple[SentOrder, ...]
    failed: tuple[FailedOrder, ...]
    halted: bool
    finished: bool
    not_attempted: int | None
    settled: bool

    @property
    def tried_tickers(self) -> frozenset[str]:
        """The tickers the batch tried to trade: its accepted and its failed orders."""
        return frozenset(s.order.ticker for s in self.sent) | frozenset(f.order.ticker for f in self.failed)


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineState:
    """Everything reconciliation reads from the engine's records, with the evidence of what was read: the audit log's newest hash and the ledger's SHA-256."""
    audit_records: int
    audit_head: str | None
    batches: tuple[Batch, ...]
    ledger: Mapping[int, Mapping[str, float]]
    ledger_sha256: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Expected:
    """Shares the engine's records say the account holds in one ticker, and where each part comes from."""
    quantity_shares: float
    sources: tuple[str, ...]


@dataclass(slots=True, kw_only=True)
class _Building:
    plan_hash: str
    tranche: int | None
    planned_utc: dt.datetime
    started_utc: dt.datetime
    started_line: int
    planned_orders: int
    held_before: dict[str, float]
    sent: list[SentOrder] = field(default_factory=list)
    failed: list[FailedOrder] = field(default_factory=list)
    halted: bool = False
    finished: bool = False
    not_attempted: int | None = None


def _planned_order(f: Fields) -> PlannedOrder:
    o = PlannedOrder(ticker=f.text("ticker"), quantity_shares=f.number("quantity"), notional_usd=f.number("usd"), side=f.choice("side", {"BUY", "SELL"}))
    if o.quantity_shares == 0 or o.side != ("BUY" if o.quantity_shares > 0 else "SELL") or o.notional_usd <= 0:
        f.fail(f"order {o.ticker}: side {o.side}, quantity {o.quantity_shares}, notional {o.notional_usd} is not an order the engine writes")
    return o


def _plan(f: Fields) -> Plan:
    f.skip("skipped", "k", "target_book", "held_source", "usd_total", "exposure_source", "broker", "short_fraction", "leverage")   # recorded for the owner (A10-A12); not used here
    return Plan(plan_hash=f.text("plan_hash"), tranche=f.integer_or_none("tranche"), slot=f.date_or_none("slot"), account_value_usd=f.number("account_value_usd"),
                orders=f.items("orders", _planned_order), held_shares=f.number_map_or_none("held") or {}, price_date=f.date_or_none("price_date"),
                exposure=f.number_or_none("exposure"))


def load_plan(plan_hash: str) -> Plan:
    """The plan stored as lab/paper/plans/<plan_hash>.json. Refuses a plan that is missing, not as quant.portfolio.plan writes it, or whose orders and account value
    no longer hash to its name (changed after it was written)."""
    path = P.PLANS / f"{plan_hash}.json"
    if not path.exists():
        raise EngineRecordError(f"plan {plan_hash[:12]}: no file {path}")
    try:
        raw = loads(path.read_bytes())
        plan = parse_object(raw, _plan, f"plan {plan_hash[:12]}")
    except SchemaError as e:
        raise EngineRecordError(str(e)) from e
    if not isinstance(raw, dict) or plan.plan_hash != plan_hash or P.plan_hash(raw["orders"], raw["account_value_usd"]) != plan_hash:
        raise EngineRecordError(f"plan {plan_hash[:12]}: its orders and account value do not hash to its name (changed after it was written)")
    return plan


def _event(rec: object, line: int, plans: dict[str, tuple[dt.datetime, int | None, int]], batches: dict[str, _Building], settled: set[str]) -> None:
    """Read one audit record into `plans`, `batches` and `settled`."""
    f = Fields(rec, f"audit line {line}")
    utc, event = f.timestamp("utc"), f.choice("event", EVENTS)
    f.text_or_none("prev")                                  # absent on records written before the chain began
    if event == "refused":
        f.text_or_none("plan_hash")
        f.text("reason")
        f.close()
        return
    h = f.text("plan_hash")
    if event == "plan":
        f.text_or_none("file")                              # file, tranche and slot may be absent, as the engine allows; each is checked when present,
        n = f.integer("orders")                             # so the engine's own reading of the tranche below cannot fail
        f.integer_or_none("tranche")
        f.date_or_none("slot")
        f.text_or_none("broker")                            # PAPER A10: the broker the plan was made for (absent on records written before A10)
        plans.setdefault(h, (utc, tranche_of_plan(cast("dict[str, object]", rec)), n))   # dated by its first record, tranche as the engine takes it
    elif event == "send_started":
        f.text("token")
        n = f.integer("orders")
        held = f.number_map("held_before")
        f.text_or_none("broker")                            # PAPER A10 (absent before A10)
        if h not in plans:
            raise EngineRecordError(f"audit line {line}: plan {h[:12]} started without a plan record")
        if h in batches:
            raise EngineRecordError(f"audit line {line}: plan {h[:12]} started a second time (line {batches[h].started_line} started it)")
        planned_utc, tranche, _ = plans[h]
        batches[h] = _Building(plan_hash=h, tranche=tranche, planned_utc=planned_utc, started_utc=utc, started_line=line, planned_orders=n, held_before=held)
    elif event == "reconciled":
        if f.integer("differences") == 0:
            settled.add(h)
    elif event == "ledger_updated":
        f.integer("tranche")
        f.integer("lines")
    else:
        b = batches.get(h)
        if b is None:
            raise EngineRecordError(f"audit line {line}: {event} for plan {h[:12]}, which was never started")
        if event == "sent":
            b.sent.append(SentOrder(plan_hash=h, order=f.record("order", _planned_order), sent_utc=utc, audit_line=line, response=f.value("response")))
        elif event == "error":
            b.failed.append(FailedOrder(plan_hash=h, order=f.record("order", _planned_order), failed_utc=utc, audit_line=line, error=f.text("error")))
        elif event == "halted_by_kill_switch":
            f.integer("sent")
            b.halted = True
        else:                                                # send_finished
            accepted, errors, b.not_attempted = f.integer("accepted"), f.integer("errors"), f.integer("not_attempted")
            if (accepted, errors) != (len(b.sent), len(b.failed)):
                raise EngineRecordError(f"audit line {line}: plan {h[:12]} finished with {accepted} accepted and {errors} failed, but the log records "
                                        f"{len(b.sent)} and {len(b.failed)}")
            b.finished = True
    f.close()


def read_ledger() -> tuple[dict[int, dict[str, float]], str | None]:
    """The tranche ledger (quant.execution.ledger) with integer tranche keys, and the SHA-256 of its file (None if there is none)."""
    out: dict[int, dict[str, float]] = {}
    for key, holding in LG.load().items():
        if not key.isdigit():
            raise EngineRecordError(f"ledger: tranche key {key!r} is not a tranche number")
        try:
            out[int(key)] = as_number_map(holding, f"ledger.{key}")
        except SchemaError as e:
            raise EngineRecordError(str(e)) from e
    return out, (hashlib.sha256(LG.LEDGER.read_bytes()).hexdigest() if LG.LEDGER.exists() else None)


def read_engine_state() -> EngineState:
    """The batches of the audit log and the ledger. Refuses an audit log whose hash chain is broken: its records cannot be trusted, and the engine refuses to
    send on it for the same reason."""
    v = A.verify()
    if v["broken"]:
        raise EngineRecordError(f"audit log chain broken at line(s) {v['broken']}: its records are not read")
    plans: dict[str, tuple[dt.datetime, int | None, int]] = {}
    building: dict[str, _Building] = {}
    settled: set[str] = set()
    for line, rec in enumerate(A.records(), start=1):
        try:
            _event(rec, line, plans, building, settled)
        except SchemaError as e:
            raise EngineRecordError(str(e)) from e
    batches = tuple(Batch(plan_hash=b.plan_hash, tranche=b.tranche, planned_utc=b.planned_utc, started_utc=b.started_utc, started_line=b.started_line,
                          planned_orders=b.planned_orders, held_before=b.held_before, sent=tuple(b.sent), failed=tuple(b.failed), halted=b.halted,
                          finished=b.finished, not_attempted=b.not_attempted, settled=b.plan_hash in settled)
                    for b in sorted(building.values(), key=lambda b: b.started_line))
    ledger, ledger_sha = read_ledger()
    return EngineState(audit_records=v["records"], audit_head=v["head"], batches=batches, ledger=ledger, ledger_sha256=ledger_sha)


def expected_holdings(state: EngineState) -> dict[str, Expected]:
    """Shares per ticker the engine's records say the account holds: the ledger's tranches summed (quant.execution.ledger.aggregate) plus the orders the broker
    accepted in batches not yet settled (a settled batch is in the ledger already). A ticker every tranche has sold appears with 0."""
    qty = LG.aggregate({str(t): dict(h) for t, h in state.ledger.items()})
    sources: dict[str, list[str]] = {t: [f"ledger tranche {i}" for i, h in sorted(state.ledger.items()) if h.get(t)] for t in qty}
    for b in state.batches:
        if b.settled:
            continue
        for s in b.sent:
            t = s.order.ticker
            qty[t] = round(qty.get(t, 0.0) + s.order.quantity_shares, QTY_DECIMALS)
            sources.setdefault(t, []).append(f"plan {b.plan_hash[:12]}: order accepted, audit line {s.audit_line}")
    return {t: Expected(quantity_shares=qty[t], sources=tuple(sources[t])) for t in sorted(qty)}
