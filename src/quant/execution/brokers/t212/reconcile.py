"""Reconciliation of the practice account against the engine's records (lab/t212/INTEGRATION_SPEC.md, "Reconciliation"). Read-only: the engine's audit log,
plans and ledger are read through quant.execution.brokers.t212.engine_records, the broker's state from the read journal (or a fresh read, journaled); a report is written and
nothing else: no engine record is written and nothing is sent.

    tools/py -m quant.execution.brokers.t212.reconcile                             # the newest account summary and positions in the journal (no network)
    dotenvx run -- tools/py -m quant.execution.brokers.t212.reconcile --live       # read them now (GET, journaled), then reconcile
    -> data/raw_ext/t212_reports/reconcile_<broker time>.json and .md; exit code 0 clean, 1 breaks, 2 refused (inputs that cannot be reconciled)

Expected holdings (quant.execution.brokers.t212.engine_records.expected_holdings): the ledger's tranches plus the orders the broker accepted in batches not yet settled, which is
how the engine's own reconciliation counts (quant.execution.engine.reconcile_sent). The report is a pure function of its inputs (`reconcile`): the same records, broker
state, time and tolerances give the same report.

Breaks (any one makes the verdict "breaks"):
  quantity_breaks       a ticker whose broker quantity differs from the expected by more than the tolerance, unless a batch that traded it is still within the
                        settlement window (then it is awaiting_settlement, information)
  unknown_positions     a position the engine's records do not explain
  price_breaks          a position opened by one accepted buy whose average price is further from the plan's price than the tolerance (a unit, currency or
                        instrument error: execution quality is quant.execution.brokers.t212.fills), or held in another currency than the plan's USD
  orders_rejected       in a batch not yet settled: an order whose submission failed, or that the broker answered REJECTED or CANCELLED
  orders_stuck          in a batch not yet settled and past the settlement window: an accepted order still pending at the broker, or, while pending orders are
                        not read (INTEGRATION_SPEC.md), one whose answer was not final and whose ticker shows a quantity break the way an order not executed
                        would (less held than expected after a buy, more after a sale)
  unparseable_answers   a broker answer in the audit log that is not an order of the documented schema
  cash_breaks           negative cash, cash reserved for orders while no order is pending, an account currency other than the plans assume
Information: matched positions, positions awaiting settlement, every price check (and why one could not be made), planned orders not executed (intent gaps),
every batch, the rejection rate over all batches (SPEC pass rule 1; a submission that failed counts as rejected: the broker did not accept the order), and two
consistency gaps of the broker's own numbers: total value less cash and investments (0 if totalValue is the whole account; its documentation says
"Investments value", so this is not a break) and the positions' wallet values less the summary's investments.
"""
from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant.execution.brokers.t212 import journal, reports, snapshots
from quant.execution.brokers.t212.api import auth
from quant.execution.brokers.t212.client import ACCOUNT_SUMMARY, POSITIONS, ReadClient, ReadError, live_client
from quant.execution.brokers.t212.engine_records import QTY_DECIMALS, Batch, EngineRecordError, EngineState, Plan, SentOrder, expected_holdings, load_plan, read_engine_state
from quant.execution.brokers.t212.records import ORDER_FINAL_STATUSES, AccountSummary, Order, Position, parse_account_summary, parse_order
from quant.execution.brokers.t212.schema import SchemaError

PLAN_CURRENCY = "USD"                                       # quant.portfolio.plan prices orders in USD (`usd`, `account_value_usd`)
ACCOUNT_CURRENCY = "GBP"                                    # lab/paper/SPEC.md section 4: the account's total value is in GBP
MAX_SNAPSHOT_SKEW = dt.timedelta(minutes=15)                # a summary and positions read further apart than this are not one state of the account
MAX_TABLE_ROWS = 50                                         # rows per table in the Markdown summary; the JSON has them all


class ReconcileInputError(Exception):
    """Inputs that cannot be reconciled: no broker state, a summary and positions read too far apart, a ticker listed twice, a plan missing for an open batch."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Tolerances:
    quantity_shares: float = 1e-3                           # quant.execution.engine.reconcile's tolerance
    price_bp: float = 1000.0                                # a break, not slippage: catches units (GBX against USD is 100x), currency and instrument errors
    settle_business_days: int = 2                           # lab/paper/SPEC.md pass rule 1: zero unreconciled differences after two business days
    cash_account_ccy: float = 0.01

    def to_json(self) -> dict[str, float | int]:
        return {"quantity_shares": self.quantity_shares, "price_bp": self.price_bp, "settle_business_days": self.settle_business_days,
                "cash_account_ccy": self.cash_account_ccy}


DEFAULT_TOLERANCES = Tolerances()


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerState:
    """The account as the broker reported it: summary and positions (one state: read at most MAX_SNAPSHOT_SKEW apart), pending orders if they were read (None:
    not read), and the journal records each came from."""
    as_of_utc: dt.datetime
    summary: AccountSummary
    positions: tuple[Position, ...]
    pending_orders: tuple[Order, ...] | None
    sources: Mapping[str, tuple[str, ...]]


def _n(x: float, places: int) -> float:
    return round(x, places) + 0.0                           # + 0.0 turns -0.0 into 0.0


def business_days_after(start_utc: dt.datetime, end_utc: dt.datetime) -> int:
    """Weekdays d with start date < d <= end date, on UTC dates. No holiday calendar: a market holiday counts as a business day."""
    d0, d1 = start_utc.astimezone(dt.UTC).date(), end_utc.astimezone(dt.UTC).date()
    return sum(1 for k in range(1, (d1 - d0).days + 1) if (d0 + dt.timedelta(days=k)).weekday() < 5)


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionLine:
    """One ticker compared: `kind` is matched, awaiting_settlement, or the break: missing (nothing held), short, excess, negative."""
    ticker: str
    kind: str
    expected_shares: float
    broker_shares: float
    sources: tuple[str, ...]
    orders: tuple[str, ...]                                  # order issues on this ticker in open batches (see OrderIssue.kind)

    @property
    def difference_shares(self) -> float:
        return _n(self.broker_shares - self.expected_shares, QTY_DECIMALS)

    def to_json(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "kind": self.kind, "expected_shares": _n(self.expected_shares, QTY_DECIMALS), "broker_shares": _n(self.broker_shares, QTY_DECIMALS),
                "difference_shares": self.difference_shares, "expected_from": list(self.sources), "order_issues": list(self.orders)}


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownPosition:
    ticker: str
    broker_shares: float
    average_price_instrument_ccy: float
    instrument_currency: str
    current_value_wallet_ccy: float

    def to_json(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "broker_shares": _n(self.broker_shares, QTY_DECIMALS), "average_price_instrument_ccy": _n(self.average_price_instrument_ccy, 4),
                "instrument_currency": self.instrument_currency, "current_value_wallet_ccy": _n(self.current_value_wallet_ccy, 2)}


@dataclass(frozen=True, slots=True, kw_only=True)
class PriceCheck:
    """A position's average price against the plan's price: `status` ok, break or unchecked (with the reason)."""
    ticker: str
    status: str
    reason: str
    plan_hash: str | None
    plan_price_usd: float | None
    plan_price_bound_bp: float | None
    broker_average_price_instrument_ccy: float | None
    instrument_currency: str | None
    deviation_bp: float | None

    def to_json(self) -> dict[str, Any]:
        def opt(x: float | None, places: int) -> float | None:
            return None if x is None else _n(x, places)
        return {"ticker": self.ticker, "status": self.status, "reason": self.reason, "plan_hash": self.plan_hash, "plan_price_usd": opt(self.plan_price_usd, 4),
                "plan_price_bound_bp": opt(self.plan_price_bound_bp, 2), "broker_average_price_instrument_ccy": opt(self.broker_average_price_instrument_ccy, 4),
                "instrument_currency": self.instrument_currency, "deviation_bp": opt(self.deviation_bp, 2)}


REJECTED_KINDS = ("failed_at_submission", "rejected_by_broker", "cancelled_by_broker")
STUCK_KINDS = ("stuck_pending_at_broker", "stuck_inferred")


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderIssue:
    """An order of an open batch that did not simply fill. `kind`: failed_at_submission (an `error` audit record), rejected_by_broker or cancelled_by_broker
    (its answer), stuck_pending_at_broker (in the pending orders past the window), stuck_inferred (answer not final, quantity break, past the window), or
    pending_within_window (information)."""
    plan_hash: str
    ticker: str
    quantity_shares: float
    at_utc: dt.datetime
    audit_line: int
    kind: str
    status: str | None
    age_business_days: int
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "ticker": self.ticker, "quantity_shares": _n(self.quantity_shares, QTY_DECIMALS), "at_utc": self.at_utc.isoformat(),
                "audit_line": self.audit_line, "kind": self.kind, "status": self.status, "age_business_days": self.age_business_days, "detail": self.detail}


@dataclass(frozen=True, slots=True, kw_only=True)
class UnparseableAnswer:
    plan_hash: str
    ticker: str
    audit_line: int
    error: str

    def to_json(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "ticker": self.ticker, "audit_line": self.audit_line, "error": self.error}


@dataclass(frozen=True, slots=True, kw_only=True)
class CashFinding:
    check: str
    detail: str
    is_break: bool

    def to_json(self) -> dict[str, Any]:
        return {"check": self.check, "detail": self.detail}


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentGap:
    """A planned order of an open batch that was not executed: its submission failed, it was not attempted, or the batch did not finish."""
    plan_hash: str
    ticker: str
    quantity_shares: float
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "ticker": self.ticker, "quantity_shares": _n(self.quantity_shares, QTY_DECIMALS), "reason": self.reason}


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchLine:
    plan_hash: str
    tranche: int | None
    planned_utc: dt.datetime
    started_utc: dt.datetime
    orders_planned: int
    accepted: int
    failed: int
    not_attempted: int | None
    halted: bool
    finished: bool
    settled: bool
    age_business_days: int

    def to_json(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "tranche": self.tranche, "planned_utc": self.planned_utc.isoformat(), "started_utc": self.started_utc.isoformat(),
                "orders_planned": self.orders_planned, "accepted": self.accepted, "failed": self.failed, "not_attempted": self.not_attempted, "halted": self.halted,
                "finished": self.finished, "settled": self.settled, "age_business_days": self.age_business_days}


@dataclass(frozen=True, slots=True, kw_only=True)
class Report:
    generated_utc: dt.datetime
    broker_as_of_utc: dt.datetime
    positions: tuple[PositionLine, ...]
    unknown_positions: tuple[UnknownPosition, ...]
    price_checks: tuple[PriceCheck, ...]
    order_issues: tuple[OrderIssue, ...]
    unparseable_answers: tuple[UnparseableAnswer, ...]
    cash: tuple[CashFinding, ...]
    intent_gaps: tuple[IntentGap, ...]
    batches: tuple[BatchLine, ...]
    rejection_rate: float | None                            # (failed at submission + REJECTED + CANCELLED answers) / orders, over all batches; None if none
    orders_counted_for_rate: int
    total_value_gap_account_ccy: float                      # totalValue - (cash available + reserved + in pies + investments' current value)
    positions_value_gap_account_ccy: float | None           # sum of positions' current wallet value - investments' current value; None: a wallet in another currency
    pending_orders_read: bool
    tolerances: Tolerances
    account_currency_expected: str
    account_currency_reported: str
    sources: Mapping[str, Any]

    def quantity_breaks(self) -> tuple[PositionLine, ...]:
        return tuple(p for p in self.positions if p.kind not in ("matched", "awaiting_settlement"))

    def price_breaks(self) -> tuple[PriceCheck, ...]:
        return tuple(c for c in self.price_checks if c.status == "break")

    def orders_rejected(self) -> tuple[OrderIssue, ...]:
        return tuple(o for o in self.order_issues if o.kind in REJECTED_KINDS)

    def orders_stuck(self) -> tuple[OrderIssue, ...]:
        return tuple(o for o in self.order_issues if o.kind in STUCK_KINDS)

    def cash_breaks(self) -> tuple[CashFinding, ...]:
        return tuple(c for c in self.cash if c.is_break)

    def break_counts(self) -> dict[str, int]:
        return {"quantity_breaks": len(self.quantity_breaks()), "unknown_positions": len(self.unknown_positions), "price_breaks": len(self.price_breaks()),
                "orders_rejected": len(self.orders_rejected()), "orders_stuck": len(self.orders_stuck()), "unparseable_answers": len(self.unparseable_answers),
                "cash_breaks": len(self.cash_breaks())}

    @property
    def breaks(self) -> int:
        return sum(self.break_counts().values())

    @property
    def verdict(self) -> str:
        return "breaks" if self.breaks else "clean"

    def to_json(self) -> dict[str, Any]:
        info_counts = {"matched": sum(p.kind == "matched" for p in self.positions), "awaiting_settlement": sum(p.kind == "awaiting_settlement" for p in self.positions),
                       "prices_unchecked": sum(c.status == "unchecked" for c in self.price_checks), "intent_gaps": len(self.intent_gaps)}
        return {
            "kind": "t212_reconciliation", "version": 1, "generated_utc": self.generated_utc.isoformat(), "broker_as_of_utc": self.broker_as_of_utc.isoformat(),
            "verdict": self.verdict, "counts": {**self.break_counts(), **info_counts},
            "quantity_breaks": [p.to_json() for p in self.quantity_breaks()], "unknown_positions": [u.to_json() for u in self.unknown_positions],
            "price_breaks": [c.to_json() for c in self.price_breaks()], "orders_rejected": [o.to_json() for o in self.orders_rejected()],
            "orders_stuck": [o.to_json() for o in self.orders_stuck()], "unparseable_answers": [u.to_json() for u in self.unparseable_answers],
            "cash_breaks": [c.to_json() for c in self.cash_breaks()],
            "matched": [p.to_json() for p in self.positions if p.kind == "matched"],
            "awaiting_settlement": [p.to_json() for p in self.positions if p.kind == "awaiting_settlement"],
            "price_checks": [c.to_json() for c in self.price_checks], "orders_pending": [o.to_json() for o in self.order_issues if o.kind == "pending_within_window"],
            "intent_gaps": [g.to_json() for g in self.intent_gaps], "batches": [b.to_json() for b in self.batches],
            "information": {"rejection_rate": None if self.rejection_rate is None else _n(self.rejection_rate, 4), "orders_counted_for_rate": self.orders_counted_for_rate,
                            "total_value_gap_account_ccy": _n(self.total_value_gap_account_ccy, 2),
                            "positions_value_gap_account_ccy": None if self.positions_value_gap_account_ccy is None else _n(self.positions_value_gap_account_ccy, 2),
                            "pending_orders_read": self.pending_orders_read, "cash_notes": [c.to_json() for c in self.cash if not c.is_break]},
            "tolerances": self.tolerances.to_json(), "account_currency_expected": self.account_currency_expected,
            "account_currency_reported": self.account_currency_reported, "sources": dict(self.sources),
        }

    def to_markdown(self) -> str:
        c, s = self.break_counts(), self.sources
        journal_refs = ", ".join(f"{k} {v[0][:12]}" for k, v in sorted(s.get("journal", {}).items()) if v) or "none"
        lines = ["# Trading 212 reconciliation", "",
                 f"Generated {self.generated_utc.isoformat()} from the broker state of {self.broker_as_of_utc.isoformat()} (journal: {journal_refs}), the audit log "
                 f"({s.get('audit_records')} records, head {str(s.get('audit_head'))[:12]}) and the ledger ({str(s.get('ledger_sha256'))[:12]}).", "",
                 f"**Verdict: {self.verdict}" + (f" ({self.breaks})" if self.breaks else "") + ".** "
                 + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in c.items()) + ". "
                 f"Matched {sum(p.kind == 'matched' for p in self.positions)}; awaiting settlement {sum(p.kind == 'awaiting_settlement' for p in self.positions)}; "
                 f"prices unchecked {sum(x.status == 'unchecked' for x in self.price_checks)}; pending orders read: {'yes' if self.pending_orders_read else 'no'}."]
        rows = ([("quantity: " + p.kind, p.ticker, f"expected {p.expected_shares:g}, broker {p.broker_shares:g} ({p.difference_shares:+g}); from "
                  f"{'; '.join(p.sources) or 'nothing'}" + (f"; orders: {', '.join(p.orders)}" if p.orders else "")) for p in self.quantity_breaks()]
                + [("unknown position", u.ticker, f"{u.broker_shares:g} shares at {u.average_price_instrument_ccy:g} {u.instrument_currency}") for u in self.unknown_positions]
                + [("price", x.ticker, x.reason + ("" if x.deviation_bp is None else f"; deviation {x.deviation_bp:+.1f} bp")) for x in self.price_breaks()]
                + [(f"order: {o.kind}", o.ticker, f"plan {o.plan_hash[:12]}, audit line {o.audit_line}, {o.detail}") for o in (*self.orders_rejected(), *self.orders_stuck())]
                + [("unparseable answer", u.ticker, f"plan {u.plan_hash[:12]}, audit line {u.audit_line}: {u.error}") for u in self.unparseable_answers]
                + [("cash: " + x.check, "", x.detail) for x in self.cash_breaks()])
        if rows:
            lines += ["", "## Breaks", "", "| Category | Ticker | Detail |", "| --- | --- | --- |"]
            lines += [f"| {a} | {b} | {d.replace('|', '/')} |" for a, b, d in rows[:MAX_TABLE_ROWS]]
            if len(rows) > MAX_TABLE_ROWS:
                lines.append(f"\n... and {len(rows) - MAX_TABLE_ROWS} more in the JSON report.")
        if self.batches:
            lines += ["", "## Batches", "", "| Plan | Tranche | Started (UTC) | Accepted | Failed | Not attempted | Settled |", "| --- | --- | --- | --- | --- | --- | --- |"]
            lines += [f"| {b.plan_hash[:12]} | {b.tranche} | {b.started_utc.isoformat()} | {b.accepted} | {b.failed} | {b.not_attempted} | {'yes' if b.settled else 'no'} |"
                      for b in self.batches[-MAX_TABLE_ROWS:]]
        rate = "none sent" if self.rejection_rate is None else f"{self.rejection_rate:.2%} of {self.orders_counted_for_rate} order(s)"
        lines += ["", f"Rejection rate over all batches: {rate} (SPEC pass rule 1: below 2%). Total value less cash and investments: {self.total_value_gap_account_ccy:.2f} "
                  f"{self.account_currency_reported}."]
        return "\n".join(lines) + "\n"


def _sent_issue(s: SentOrder, now_utc: dt.datetime, kind: str, status: str, detail: str) -> OrderIssue:
    return OrderIssue(plan_hash=s.plan_hash, ticker=s.order.ticker, quantity_shares=s.order.quantity_shares, at_utc=s.sent_utc, audit_line=s.audit_line, kind=kind,
                      status=status, age_business_days=business_days_after(s.sent_utc, now_utc), detail=detail)


def _answers(batch: Batch) -> list[tuple[int, Order | str]]:
    """Each accepted order's answer parsed as a documented order, or the error that says why it is not one, by audit line."""
    out: list[tuple[int, Order | str]] = []
    for s in batch.sent:
        try:
            out.append((s.audit_line, parse_order(s.response, f"audit line {s.audit_line}.response")))
        except SchemaError as e:
            out.append((s.audit_line, str(e)))
    return out


def _order_issues(open_batches: Sequence[Batch], pending: tuple[Order, ...] | None, now_utc: dt.datetime,
                  tol: Tolerances) -> tuple[list[OrderIssue], list[UnparseableAnswer], list[tuple[str, OrderIssue]]]:
    """(issues, unparseable answers, candidates for stuck_inferred keyed by ticker) for the orders of open batches."""
    issues: list[OrderIssue] = []
    unparseable: list[UnparseableAnswer] = []
    candidates: list[tuple[str, OrderIssue]] = []
    pending_ids = None if pending is None else {o.order_id for o in pending}
    for b in open_batches:
        answers = dict(_answers(b))
        for s in b.sent:
            age = business_days_after(s.sent_utc, now_utc)
            answer = answers[s.audit_line]
            if isinstance(answer, str):
                unparseable.append(UnparseableAnswer(plan_hash=b.plan_hash, ticker=s.order.ticker, audit_line=s.audit_line, error=answer))
            elif answer.status in ("REJECTED", "CANCELLED"):
                issues.append(_sent_issue(s, now_utc, "rejected_by_broker" if answer.status == "REJECTED" else "cancelled_by_broker", answer.status,
                                          f"broker order {answer.order_id} answered {answer.status}"))
            elif answer.status in ORDER_FINAL_STATUSES:
                continue
            elif pending_ids is None:
                candidates.append((s.order.ticker, _sent_issue(s, now_utc, "stuck_inferred", answer.status,
                                                               f"broker order {answer.order_id} answered {answer.status} (not final) {age} business day(s) ago, and the "
                                                               "position does not reflect it; pending orders are not read")))
            elif answer.order_id in pending_ids:
                issues.append(_sent_issue(s, now_utc, "stuck_pending_at_broker" if age >= tol.settle_business_days else "pending_within_window", answer.status,
                                          f"broker order {answer.order_id} still pending after {age} business day(s)"))
        for f in b.failed:
            issues.append(OrderIssue(plan_hash=b.plan_hash, ticker=f.order.ticker, quantity_shares=f.order.quantity_shares, at_utc=f.failed_utc, audit_line=f.audit_line,
                                     kind="failed_at_submission", status=None, age_business_days=business_days_after(f.failed_utc, now_utc), detail=f.error))
    return issues, unparseable, candidates


def _price_check(s: SentOrder, pos: Position | None, status: str, reason: str, deviation_bp: float | None = None) -> PriceCheck:
    return PriceCheck(ticker=s.order.ticker, status=status, reason=reason, plan_hash=s.plan_hash, plan_price_usd=s.order.plan_price_usd,
                      plan_price_bound_bp=s.order.plan_price_bound_bp, broker_average_price_instrument_ccy=None if pos is None else pos.average_price_paid_instrument_ccy,
                      instrument_currency=None if pos is None else pos.instrument.currency, deviation_bp=deviation_bp)


def _price_checks(open_batches: Sequence[Batch], held: Mapping[str, Position], tol: Tolerances) -> list[PriceCheck]:
    trades: dict[str, list[tuple[Batch, SentOrder]]] = {}
    for b in open_batches:
        for s in b.sent:
            trades.setdefault(s.order.ticker, []).append((b, s))
    out = []
    for t in sorted(trades):
        (b, s), pos = trades[t][0], held.get(t)
        if len(trades[t]) > 1 or s.order.side != "BUY":
            out.append(_price_check(s, pos, "unchecked", "more than one order, or a sale, in the open batches: the average price paid does not isolate one buy"))
        elif b.held_before.get(t, 0.0) > tol.quantity_shares:
            out.append(_price_check(s, pos, "unchecked", "the position existed before the batch: its average price mixes earlier shares"))
        elif pos is None or pos.quantity_shares <= tol.quantity_shares:
            out.append(_price_check(s, pos, "unchecked", "nothing held to compare (see the quantity breaks)"))
        elif pos.instrument.currency != PLAN_CURRENCY:
            out.append(_price_check(s, pos, "break", f"held in {pos.instrument.currency}; the plan's price is {PLAN_CURRENCY}"))
        else:
            dev = (pos.average_price_paid_instrument_ccy / s.order.plan_price_usd - 1.0) * 1e4
            out.append(_price_check(s, pos, "break" if abs(dev) > tol.price_bp else "ok", f"average price paid against the plan price, tolerance {tol.price_bp:g} bp",
                                    dev))
    return out


def _cash(summary: AccountSummary, positions: Sequence[Position], pending: tuple[Order, ...] | None, tol: Tolerances,
          account_currency: str) -> tuple[list[CashFinding], float, float | None]:
    c = summary.cash
    found = []
    if summary.account_currency != account_currency:
        found.append(CashFinding(check="account_currency", detail=f"the account currency is {summary.account_currency}; the plans assume {account_currency}", is_break=True))
    if c.available_to_trade_account_ccy < -tol.cash_account_ccy:
        found.append(CashFinding(check="negative_cash", detail=f"available to trade {c.available_to_trade_account_ccy:.2f} {summary.account_currency}", is_break=True))
    if c.reserved_for_orders_account_ccy > tol.cash_account_ccy:
        if pending is not None and not pending:
            found.append(CashFinding(check="reserved_without_pending", detail=f"{c.reserved_for_orders_account_ccy:.2f} {summary.account_currency} reserved for orders, "
                                                                              "but no order is pending", is_break=True))
        elif pending is None:
            found.append(CashFinding(check="reserved_cash", detail=f"{c.reserved_for_orders_account_ccy:.2f} {summary.account_currency} reserved for orders; pending "
                                                                   "orders are not read", is_break=False))
    gap = summary.total_value_account_ccy - (c.available_to_trade_account_ccy + c.reserved_for_orders_account_ccy + c.in_pies_account_ccy
                                             + summary.investments.current_value_account_ccy)
    same_ccy = all(p.wallet_impact.currency == summary.account_currency for p in positions)
    value_gap = (sum(p.wallet_impact.current_value_wallet_ccy for p in positions) - summary.investments.current_value_account_ccy) if same_ccy else None
    return found, gap, value_gap


def _intent_gaps(open_batches: Sequence[Batch], plans: Mapping[str, Plan]) -> list[IntentGap]:
    out = [IntentGap(plan_hash=b.plan_hash, ticker=f.order.ticker, quantity_shares=f.order.quantity_shares, reason="submission failed")
           for b in open_batches for f in b.failed]
    for b in open_batches:
        plan = plans.get(b.plan_hash)
        if plan is None:
            raise ReconcileInputError(f"plan {b.plan_hash[:12]} of an open batch was not given")
        attempted = b.tried_tickers
        rest = [o for o in plan.orders if o.ticker not in attempted]
        if b.finished and len(rest) != b.not_attempted:
            raise ReconcileInputError(f"plan {b.plan_hash[:12]}: the audit log says {b.not_attempted} order(s) not attempted, the plan leaves {len(rest)}")
        reason = ("not attempted: stopped by the kill switch" if b.halted else "not attempted") if b.finished else "the batch did not finish (no send_finished record)"
        out += [IntentGap(plan_hash=b.plan_hash, ticker=o.ticker, quantity_shares=o.quantity_shares, reason=reason) for o in rest]
    return sorted(out, key=lambda g: (g.plan_hash, g.ticker))


def _rejection_rate(batches: Sequence[Batch]) -> tuple[float | None, int]:
    rejected = total = 0
    for b in batches:
        total += len(b.sent) + len(b.failed)
        rejected += len(b.failed) + sum(1 for _, a in _answers(b) if isinstance(a, Order) and a.status in ("REJECTED", "CANCELLED"))
    return (rejected / total if total else None), total


def reconcile(state: EngineState, plans: Mapping[str, Plan], broker: BrokerState, now_utc: dt.datetime, tol: Tolerances | None = None,
              account_currency: str = ACCOUNT_CURRENCY) -> Report:
    """The reconciliation report of `broker` against the engine's records at `now_utc` (module docstring). Pure: no file, clock or network is read."""
    tol = tol or Tolerances()
    if now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    held: dict[str, Position] = {}
    for p in broker.positions:
        if p.ticker in held:
            raise ReconcileInputError(f"the broker lists {p.ticker} twice")
        held[p.ticker] = p
    open_batches = [b for b in state.batches if not b.settled]
    window = {t for b in open_batches if business_days_after(b.started_utc, now_utc) < tol.settle_business_days for t in b.tried_tickers}
    issues, unparseable, candidates = _order_issues(open_batches, broker.pending_orders, now_utc, tol)
    expected = expected_holdings(state)
    lines, unknown = [], []
    broken: dict[str, float] = {}                           # ticker -> broker less expected shares, for the quantity breaks
    for t in sorted(set(expected) | set(held)):
        e = expected[t].quantity_shares if t in expected else 0.0
        a = held[t].quantity_shares if t in held else 0.0
        if t not in expected:
            if a > tol.quantity_shares:
                p = held[t]
                unknown.append(UnknownPosition(ticker=t, broker_shares=a, average_price_instrument_ccy=p.average_price_paid_instrument_ccy,
                                               instrument_currency=p.instrument.currency, current_value_wallet_ccy=p.wallet_impact.current_value_wallet_ccy))
            continue
        d = a - e
        if abs(d) <= tol.quantity_shares:
            kind = "matched"
            if abs(e) <= tol.quantity_shares and abs(a) <= tol.quantity_shares:
                continue                                    # nothing expected, nothing held: no line
        elif t in window:
            kind = "awaiting_settlement"
        else:
            kind = "negative" if a < -tol.quantity_shares else "missing" if a <= tol.quantity_shares else "short" if d < 0 else "excess"
            broken[t] = d
        lines.append((t, kind, e, a))
    stuck = [issue for t, issue in candidates if broken.get(t, 0.0) * issue.quantity_shares < 0]      # a break the way the order not executing would leave
    issues = sorted(issues + stuck, key=lambda o: (o.plan_hash, o.audit_line))
    by_ticker: dict[str, list[str]] = {}
    for o in issues:
        by_ticker.setdefault(o.ticker, []).append(o.kind)
    positions = tuple(PositionLine(ticker=t, kind=k, expected_shares=e, broker_shares=a, sources=expected[t].sources, orders=tuple(by_ticker.get(t, ())))
                      for t, k, e, a in lines)
    cash, gap, value_gap = _cash(broker.summary, broker.positions, broker.pending_orders, tol, account_currency)
    rate, counted = _rejection_rate(state.batches)
    batch_lines = tuple(BatchLine(plan_hash=b.plan_hash, tranche=b.tranche, planned_utc=b.planned_utc, started_utc=b.started_utc, orders_planned=b.planned_orders,
                                  accepted=len(b.sent), failed=len(b.failed), not_attempted=b.not_attempted, halted=b.halted, finished=b.finished, settled=b.settled,
                                  age_business_days=business_days_after(b.started_utc, now_utc)) for b in state.batches)
    sources = {"audit_records": state.audit_records, "audit_head": state.audit_head, "ledger_sha256": state.ledger_sha256, "plans": sorted(plans),
               "journal": {k: list(v) for k, v in sorted(broker.sources.items())}}
    return Report(generated_utc=now_utc, broker_as_of_utc=broker.as_of_utc, positions=positions, unknown_positions=tuple(unknown),
                  price_checks=tuple(_price_checks(open_batches, held, tol)), order_issues=tuple(issues),
                  unparseable_answers=tuple(sorted(unparseable, key=lambda u: u.audit_line)), cash=tuple(cash), intent_gaps=tuple(_intent_gaps(open_batches, plans)),
                  batches=batch_lines, rejection_rate=rate, orders_counted_for_rate=counted, total_value_gap_account_ccy=gap, positions_value_gap_account_ccy=value_gap,
                  pending_orders_read=broker.pending_orders is not None, tolerances=tol, account_currency_expected=account_currency,
                  account_currency_reported=broker.summary.account_currency, sources=sources)


def broker_from_journal(journal_path: Path = journal.PATH) -> BrokerState:
    """The broker's state from the newest full positions read in the journal and the account summary read nearest to it in time, at most MAX_SNAPSHOT_SKEW
    apart: a summary read on its own later (quant.execution.brokers.t212.health reads one) does not displace the one read with the positions."""
    positions = snapshots.latest_positions(journal_path)
    if positions is None:
        raise ReconcileInputError(f"no positions read in {journal_path}: run python -m quant.execution.brokers.t212.pull")
    found = snapshots.summaries(journal_path)
    if not found:
        raise ReconcileInputError(f"no account summary read in {journal_path}: run python -m quant.execution.brokers.t212.pull")
    nearest = min(found, key=lambda e: abs(e.received_utc - positions.received_utc))
    skew = abs(nearest.received_utc - positions.received_utc)
    if skew > MAX_SNAPSHOT_SKEW:
        raise ReconcileInputError(f"the newest positions and the nearest account summary were read {skew} apart (more than {MAX_SNAPSHOT_SKEW}): "
                                  "run python -m quant.execution.brokers.t212.pull")
    summary = snapshots.typed(nearest, parse_account_summary)
    return BrokerState(as_of_utc=max(summary.received_utc, positions.received_utc), summary=summary.data, positions=positions.data, pending_orders=None,
                       sources={ACCOUNT_SUMMARY.name: (summary.record_sha256,), POSITIONS.name: (positions.record_sha256,)})


def broker_live(client: ReadClient) -> BrokerState:
    """The broker's state read now: account summary, then positions (both journaled by the client)."""
    summary, positions = client.account_summary(), client.positions()
    return BrokerState(as_of_utc=positions.responses[-1].received_utc, summary=summary.data, positions=positions.data, pending_orders=None,
                       sources={ACCOUNT_SUMMARY.name: summary.sources, POSITIONS.name: positions.sources})


def main(argv: list[str] | None = None, now: dt.datetime | None = None, client: ReadClient | None = None, out_dir: Path | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quant.execution.brokers.t212.reconcile", description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="read the account now (needs the read keys) instead of the newest journaled reads")
    ap.add_argument("--journal", type=Path, default=journal.PATH)
    ap.add_argument("--price-tol-bp", type=float, default=DEFAULT_TOLERANCES.price_bp)
    ap.add_argument("--settle-days", type=int, default=DEFAULT_TOLERANCES.settle_business_days)
    ap.add_argument("--currency", default=ACCOUNT_CURRENCY, help="the account currency the plans assume")
    a = ap.parse_args(argv)
    tol = Tolerances(price_bp=a.price_tol_bp, settle_business_days=a.settle_days)
    try:
        state = read_engine_state()
        plans = {b.plan_hash: load_plan(b.plan_hash) for b in state.batches if not b.settled}
        if a.live:
            auth("READ")                                    # refuse before any request when the read keys are not in the environment
            broker = broker_live(client or live_client(a.journal))
        else:
            broker = broker_from_journal(a.journal)
        report = reconcile(state, plans, broker, now or dt.datetime.now(dt.UTC), tol, a.currency)
        json_path, _ = reports.write(f"reconcile_{reports.stamp(broker.as_of_utc)}", report.to_json(), report.to_markdown(), out_dir or reports.REPORTS)
    except (EngineRecordError, ReconcileInputError, journal.JournalError, snapshots.SnapshotError, ReadError, FileExistsError, RuntimeError) as e:
        print(f"REFUSED: {e}")
        return 2
    print(report.to_markdown())
    print(json_path)
    return 1 if report.breaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
