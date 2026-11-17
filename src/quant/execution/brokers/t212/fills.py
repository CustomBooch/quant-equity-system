"""Fill and cost analysis (lab/t212/INTEGRATION_SPEC.md, "Fills and costs"): what each order the broker accepted realised against its plan (price and slippage,
fees, the FX conversion) and the tranche holdings the fills imply, in the paper ledger's format. Read-only: the engine's records through
quant.execution.brokers.t212.engine_records and the broker's responses from the read journal (quant.execution.brokers.t212.snapshots); a report is written and nothing else.

    tools/py -m quant.execution.brokers.t212.fills                                 # no network
    -> data/raw_ext/t212_reports/fills_<newest evidence time>.json and .md; exit code 0 written, 2 refused (nothing to analyse, records that cannot be read)

Evidence for an execution, best first:
  history_fill        the order-history read (journal endpoint snapshots.HISTORY_ENDPOINT), matched by the order id the broker answered: each TRADE fill's
                      price, quantity and time, and its wallet impact: net value, FX rate and each fee. This layer does not make that read yet
                      (INTEGRATION_SPEC.md, "Not included"); until it does, executions come from positions.
  position_snapshot   the earliest full positions read after a buy, and before the next batch that trades its ticker, that shows the position at the order's
                      quantity, opened after the batch started, with nothing held before the batch: its average price paid is then the fill price and its
                      total cost the cost in the wallet currency. Fees are not visible, and the implied FX rate includes any fee the broker adds to the cost.
                      A sale does not show in positions: it needs the order history.
An accepted order without an execution is listed as unmeasured, with the reason.

Units: *_shares are signed as the order (sales negative); *_usd is the plan's currency; *_instrument_ccy is in `instrument_currency` and *_wallet_ccy in
`wallet_currency`; *_bp are basis points; times are timezone-aware UTC. Slippage is signed so that positive is adverse (paid more on a buy, received less on a
sale) and is measured against the plan's price: the close of its price_date, recovered from the cent-rounded notional to within plan_price_bound_bp.
lab/paper/SPEC.md pass rule 2 names the previous close (median absolute slippage at most 5 bp, mean at most 8 bp, judged after 3 months and 12 slots):
plan_price_is_previous_close says whether the plan's price date is the weekday before the fill (no holiday calendar), and the summary's figure is
indicative. Fees are taken as absolute amounts: the sign of a fee's documented `quantity` is not stated.

The FX rate's direction is not documented: for each history fill, the reading that reproduces the wallet's net value from price x quantity within FX_MATCH is
taken (wallet_per_instrument: value x rate; instrument_per_wallet: value / rate); if neither or both do, it is undetermined and no wallet value is derived.

Ledger view: the paper ledger (lab/paper/ledger.json, as quant.execution.ledger.record_batch writes it: tranche -> ticker -> shares, zero lines left out) with the
tranche of each unsettled batch whose accepted orders are all measured set to the holdings its plan started from plus the shares its fills realised. It is
what the engine would record once the broker's positions reconcile; where it differs from the engine's expectation (held + accepted), the lines are listed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant.execution.brokers.t212 import journal, reports, snapshots
from quant.execution.brokers.t212.engine_records import QTY_DECIMALS, Batch, EngineRecordError, EngineState, Plan, SentOrder, load_plan, read_engine_state
from quant.execution.brokers.t212.records import ORDER_NOT_EXECUTED_STATUSES, Fill, HistoricalOrder, Position, parse_order
from quant.execution.brokers.t212.schema import SchemaError
from quant.execution.brokers.t212.snapshots import Snapshot

PLAN_CURRENCY = "USD"                                       # quant.portfolio.plan prices orders in USD
QTY_TOL = 1e-3                                              # shares: quant.execution.engine.reconcile's tolerance
FX_MATCH = 0.02                                             # fees are a fraction of a percent; the wrong direction is off by the rate squared
PASS_RULE_2_MEDIAN_BP, PASS_RULE_2_MEAN_BP = 5.0, 8.0       # lab/paper/SPEC.md pass rule 2
FX_FEE = "CURRENCY_CONVERSION_FEE"
MAX_TABLE_ROWS = 50                                         # rows per table in the Markdown summary; the JSON has them all


class FillsInputError(Exception):
    """Inputs that cannot be analysed: no batch, no evidence, a plan missing for a batch, a positions read that lists a ticker twice."""


def _n(x: float, places: int) -> float:
    return round(x, places) + 0.0                           # + 0.0 turns -0.0 into 0.0


def _opt(x: float | None, places: int) -> float | None:
    return None if x is None else _n(x, places)


def _previous_weekday(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


@dataclass(frozen=True, slots=True, kw_only=True)
class Execution:
    """What one accepted order realised, from one kind of evidence (module docstring)."""
    source: str                                             # history_fill or position_snapshot
    evidence: tuple[str, ...]                               # the journal records the numbers come from
    fill_ids: tuple[int, ...]                               # history fills; () for a position snapshot
    filled_shares: float                                    # signed as the order
    price_instrument_ccy: float                             # average fill price, weighted by quantity
    instrument_currency: str
    filled_at_utc: dt.datetime                              # the last fill (history) or the position's opening (snapshot)
    observed_utc: dt.datetime                               # when the evidence was received
    wallet_currency: str
    wallet_value_wallet_ccy: float                          # history: the fills' net values (absolute); snapshot: the position's total cost
    fx_rates_reported: tuple[float, ...]                    # each history fill's fxRate as the API reports it
    fx_convention: str                                      # wallet_per_instrument, instrument_per_wallet, same_currency, implied (snapshot) or undetermined
    wallet_per_instrument: float | None                     # wallet currency per unit of instrument currency, by the convention; None if undetermined
    fees_wallet_ccy: Mapping[str, float] | None             # by fee name, in the wallet currency; None: not visible (snapshot)
    fees_other_ccy: tuple[str, ...]                         # fees charged in another currency than the wallet's: listed, not summed

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "evidence": list(self.evidence), "fill_ids": list(self.fill_ids), "filled_shares": _n(self.filled_shares, QTY_DECIMALS),
                "price_instrument_ccy": _n(self.price_instrument_ccy, 6), "instrument_currency": self.instrument_currency,
                "filled_at_utc": self.filled_at_utc.isoformat(), "observed_utc": self.observed_utc.isoformat(), "wallet_currency": self.wallet_currency,
                "wallet_value_wallet_ccy": _n(self.wallet_value_wallet_ccy, 2), "fx_rates_reported": [_n(r, 8) for r in self.fx_rates_reported],
                "fx_convention": self.fx_convention, "wallet_per_instrument": _opt(self.wallet_per_instrument, 8),
                "fees_wallet_ccy": None if self.fees_wallet_ccy is None else {k: _n(v, 4) for k, v in sorted(self.fees_wallet_ccy.items())},
                "fees_other_ccy": list(self.fees_other_ccy)}


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderCost:
    """An accepted order, its plan and its execution, with the costs derived from them."""
    plan_hash: str
    tranche: int | None
    audit_line: int
    sent_utc: dt.datetime
    ticker: str
    side: str
    planned_shares: float
    plan_notional_usd: float
    plan_price_usd: float
    plan_price_bound_bp: float
    plan_price_date: dt.date | None
    execution: Execution

    @property
    def fill_ratio(self) -> float:
        return self.execution.filled_shares / self.planned_shares

    @property
    def slippage_bp(self) -> float | None:
        """Positive is adverse; None when the fill is not in the plan's currency."""
        if self.execution.instrument_currency != PLAN_CURRENCY:
            return None
        return (1.0 if self.side == "BUY" else -1.0) * (self.execution.price_instrument_ccy / self.plan_price_usd - 1.0) * 1e4

    @property
    def slippage_usd(self) -> float | None:
        if self.execution.instrument_currency != PLAN_CURRENCY:
            return None
        return (1.0 if self.side == "BUY" else -1.0) * (self.execution.price_instrument_ccy - self.plan_price_usd) * abs(self.execution.filled_shares)

    @property
    def gross_value_wallet_ccy(self) -> float | None:
        """Price x quantity in the wallet currency, by the FX convention; None if it is undetermined."""
        e = self.execution
        return None if e.wallet_per_instrument is None else abs(e.filled_shares) * e.price_instrument_ccy * e.wallet_per_instrument

    @property
    def fees_total_wallet_ccy(self) -> float | None:
        return None if self.execution.fees_wallet_ccy is None else sum(self.execution.fees_wallet_ccy.values())

    def _bp_of_gross(self, amount: float | None) -> float | None:
        gross = self.gross_value_wallet_ccy
        return None if amount is None or not gross else amount / gross * 1e4

    @property
    def fees_bp(self) -> float | None:
        return self._bp_of_gross(self.fees_total_wallet_ccy)

    @property
    def fx_fee_wallet_ccy(self) -> float | None:
        return None if self.execution.fees_wallet_ccy is None else self.execution.fees_wallet_ccy.get(FX_FEE, 0.0)

    @property
    def fx_fee_bp(self) -> float | None:
        return self._bp_of_gross(self.fx_fee_wallet_ccy)

    @property
    def all_in_cost_bp(self) -> float | None:
        s, f = self.slippage_bp, self.fees_bp
        return None if s is None or f is None else s + f

    @property
    def plan_price_is_previous_close(self) -> bool | None:
        if self.plan_price_date is None:
            return None
        return self.plan_price_date == _previous_weekday(self.execution.filled_at_utc.astimezone(dt.UTC).date())

    def to_json(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "tranche": self.tranche, "audit_line": self.audit_line, "sent_utc": self.sent_utc.isoformat(), "ticker": self.ticker,
                "side": self.side, "planned_shares": _n(self.planned_shares, QTY_DECIMALS), "plan_notional_usd": _n(self.plan_notional_usd, 2),
                "plan_price_usd": _n(self.plan_price_usd, 6), "plan_price_bound_bp": _n(self.plan_price_bound_bp, 4),
                "plan_price_date": None if self.plan_price_date is None else self.plan_price_date.isoformat(),
                "plan_price_is_previous_close": self.plan_price_is_previous_close, "fill_ratio": _n(self.fill_ratio, 6),
                "slippage_bp": _opt(self.slippage_bp, 2), "slippage_usd": _opt(self.slippage_usd, 4), "gross_value_wallet_ccy": _opt(self.gross_value_wallet_ccy, 2),
                "fees_total_wallet_ccy": _opt(self.fees_total_wallet_ccy, 4), "fees_bp": _opt(self.fees_bp, 2), "fx_fee_wallet_ccy": _opt(self.fx_fee_wallet_ccy, 4),
                "fx_fee_bp": _opt(self.fx_fee_bp, 2), "all_in_cost_bp": _opt(self.all_in_cost_bp, 2), "execution": self.execution.to_json()}


@dataclass(frozen=True, slots=True, kw_only=True)
class Unmeasured:
    plan_hash: str
    tranche: int | None
    audit_line: int
    ticker: str
    side: str
    planned_shares: float
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "tranche": self.tranche, "audit_line": self.audit_line, "ticker": self.ticker, "side": self.side,
                "planned_shares": _n(self.planned_shares, QTY_DECIMALS), "reason": self.reason}


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerDifference:
    """A line where the fills realised another quantity than the engine expects (held + accepted)."""
    tranche: int
    ticker: str
    expected_shares: float
    realised_shares: float

    def to_json(self) -> dict[str, Any]:
        return {"tranche": self.tranche, "ticker": self.ticker, "expected_shares": _n(self.expected_shares, QTY_DECIMALS),
                "realised_shares": _n(self.realised_shares, QTY_DECIMALS), "difference_shares": _n(self.realised_shares - self.expected_shares, QTY_DECIMALS)}


@dataclass(frozen=True, slots=True, kw_only=True)
class FillReport:
    generated_utc: dt.datetime
    costs: tuple[OrderCost, ...]
    unmeasured: tuple[Unmeasured, ...]
    orders_not_executed: int                                # planned orders that failed at submission or were not attempted (quant.execution.brokers.t212.reconcile lists them)
    ledger_view: Mapping[str, Mapping[str, float]]
    ledger_batches: tuple[tuple[str, str], ...]             # (plan hash, what the view did with the batch), for every unsettled batch
    ledger_differences: tuple[LedgerDifference, ...]
    tolerance_shares: float
    sources: Mapping[str, Any]

    def slippage(self) -> dict[str, Any]:
        xs = [(c.slippage_bp, c.plan_notional_usd) for c in self.costs if c.slippage_bp is not None]
        if not xs:
            return {"orders": 0, "median_abs_bp": None, "mean_abs_bp": None, "weighted_mean_bp": None, "worst_bp": None, "pass_rule_2": "not measurable"}
        med, mean = statistics.median(abs(x) for x, _ in xs), statistics.fmean(abs(x) for x, _ in xs)
        return {"orders": len(xs), "median_abs_bp": _n(med, 2), "mean_abs_bp": _n(mean, 2),
                "weighted_mean_bp": _n(sum(x * w for x, w in xs) / sum(w for _, w in xs), 2), "worst_bp": _n(max(x for x, _ in xs), 2),
                "pass_rule_2": "within" if med <= PASS_RULE_2_MEDIAN_BP and mean <= PASS_RULE_2_MEAN_BP else "outside"}

    def fees(self) -> dict[str, dict[str, float]]:
        """Fees of the history fills, wallet currency -> fee name -> total."""
        out: dict[str, dict[str, float]] = {}
        for c in self.costs:
            for name, amount in (c.execution.fees_wallet_ccy or {}).items():
                by_name = out.setdefault(c.execution.wallet_currency, {})
                by_name[name] = by_name.get(name, 0.0) + amount
        return {ccy: {k: _n(v, 4) for k, v in sorted(by_name.items())} for ccy, by_name in sorted(out.items())}

    def fx_conventions(self) -> list[str]:
        return sorted({c.execution.fx_convention for c in self.costs if c.execution.source == "history_fill"})

    def counts(self) -> dict[str, int]:
        return {"orders_accepted": len(self.costs) + len(self.unmeasured), "measured_from_history": sum(c.execution.source == "history_fill" for c in self.costs),
                "measured_from_positions": sum(c.execution.source == "position_snapshot" for c in self.costs), "unmeasured": len(self.unmeasured),
                "orders_not_executed": self.orders_not_executed}

    def to_json(self) -> dict[str, Any]:
        return {"kind": "t212_fills", "version": 1, "generated_utc": self.generated_utc.isoformat(), "counts": self.counts(), "slippage": self.slippage(),
                "fees_by_wallet_ccy": self.fees(), "fx_conventions_seen": self.fx_conventions(), "orders": [c.to_json() for c in self.costs],
                "unmeasured": [u.to_json() for u in self.unmeasured],
                "ledger_view": {k: {t: _n(q, QTY_DECIMALS) for t, q in sorted(v.items())} for k, v in sorted(self.ledger_view.items(), key=lambda kv: int(kv[0]))},
                "ledger_view_batches": [{"plan_hash": h, "status": s} for h, s in self.ledger_batches],
                "ledger_view_differences": [d.to_json() for d in self.ledger_differences],
                "parameters": {"tolerance_shares": self.tolerance_shares, "fx_match": FX_MATCH,
                               "pass_rule_2": {"median_abs_bp": PASS_RULE_2_MEDIAN_BP, "mean_abs_bp": PASS_RULE_2_MEAN_BP}},
                "sources": dict(self.sources)}

    def to_markdown(self) -> str:
        n, s, src = self.counts(), self.slippage(), self.sources

        def bp(x: float | None) -> str:
            return "-" if x is None else f"{x:+.1f}"
        fees = "; ".join(f"{ccy} " + ", ".join(f"{k} {v:g}" for k, v in by.items()) for ccy, by in self.fees().items()) or "none visible (they need the order history)"
        lines = ["# Trading 212 fills and costs", "",
                 f"Generated {self.generated_utc.isoformat()} from the audit log ({src.get('audit_records')} records, head {str(src.get('audit_head'))[:12]}), "
                 f"{len(src.get('positions_reads', []))} positions read(s) and {len(src.get('history_pages', []))} order-history page(s).", "",
                 f"**{n['measured_from_history'] + n['measured_from_positions']} of {n['orders_accepted']} accepted order(s) measured** "
                 f"({n['measured_from_history']} from the order history, {n['measured_from_positions']} from positions); {n['unmeasured']} unmeasured; "
                 f"{n['orders_not_executed']} planned order(s) not executed.",
                 "Slippage against the plan's price (positive is adverse): " + ("none measured." if not s["orders"] else
                 f"median |x| {s['median_abs_bp']:.2f} bp, mean |x| {s['mean_abs_bp']:.2f} bp, value-weighted mean {s['weighted_mean_bp']:+.2f} bp, worst "
                 f"{s['worst_bp']:+.2f} bp over {s['orders']} order(s).") + f" Pass rule 2 (indicative): {s['pass_rule_2']}.",
                 f"Fees: {fees}. FX convention of the history fills: {', '.join(self.fx_conventions()) or 'no history fill'}."]
        if self.costs:
            lines += ["", "## Orders", "", "| Plan | Ticker | Side | Planned | Filled | Plan price (USD) | Fill price | Slippage (bp) | Fees (bp) | Source |",
                      "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
            lines += [f"| {c.plan_hash[:12]} | {c.ticker} | {c.side} | {c.planned_shares:g} | {c.execution.filled_shares:g} | {c.plan_price_usd:.4f} | "
                      f"{c.execution.price_instrument_ccy:.4f} {c.execution.instrument_currency} | {bp(c.slippage_bp)} | {bp(c.fees_bp)} | {c.execution.source} |"
                      for c in self.costs[-MAX_TABLE_ROWS:]]
        if self.unmeasured:
            lines += ["", "## Unmeasured", "", "| Plan | Ticker | Side | Shares | Reason |", "| --- | --- | --- | --- | --- |"]
            lines += [f"| {u.plan_hash[:12]} | {u.ticker} | {u.side} | {u.planned_shares:g} | {u.reason.replace('|', '/')} |" for u in self.unmeasured[-MAX_TABLE_ROWS:]]
        if len(self.costs) > MAX_TABLE_ROWS or len(self.unmeasured) > MAX_TABLE_ROWS:
            lines.append(f"\nThe tables show the newest {MAX_TABLE_ROWS} rows; the JSON report has them all.")
        lines += ["", "## Ledger view", ""]
        lines += [f"- plan {h[:12]}: {st}" for h, st in self.ledger_batches] or ["- no unsettled batch: the view is the recorded ledger"]
        lines += [f"- tranche {d.tranche} {d.ticker}: fills realised {d.realised_shares:g} shares, the engine expects {d.expected_shares:g}" for d in self.ledger_differences]
        return "\n".join(lines) + "\n"


def _answer_id(s: SentOrder) -> int | None:
    """The order id the broker answered, or None if the answer is not a documented order (quant.execution.brokers.t212.reconcile reports it)."""
    try:
        return parse_order(s.response).order_id
    except SchemaError:
        return None


def _fx(fill: Fill, instrument_currency: str) -> str:
    """Which reading of the fill's fxRate reproduces its wallet net value (module docstring)."""
    w = fill.wallet_impact
    if w.currency == instrument_currency:
        return "same_currency"
    value, net, r = abs(fill.quantity_shares) * fill.price_instrument_ccy, abs(w.net_value_wallet_ccy), w.fx_rate
    if r <= 0 or value <= 0:
        return "undetermined"
    fits = [name for name, wallet in (("wallet_per_instrument", value * r), ("instrument_per_wallet", value / r)) if abs(net / wallet - 1.0) <= FX_MATCH]
    return fits[0] if len(fits) == 1 else "undetermined"


def _from_history(s: SentOrder, found: Sequence[tuple[HistoricalOrder, str, dt.datetime]]) -> Execution | str | None:
    """The execution the order history records for `s`; a reason if it says the order did not execute or contradicts the plan; None if it has no fill yet."""
    for h, _, _ in found:
        if (h.order.side, h.order.ticker) != (s.order.side, s.order.ticker):
            return f"the order history's order {h.order.order_id} is a {h.order.side} of {h.order.ticker}; the plan's is a {s.order.side} of {s.order.ticker}"
    trades: dict[int, tuple[Fill, str, dt.datetime]] = {}
    for h, sha, received in found:
        if h.fill is not None and h.fill.fill_type == "TRADE":
            trades.setdefault(h.fill.fill_id, (h.fill, sha, received))
    if not trades:
        statuses = sorted({h.order.status for h, _, _ in found})
        return f"the order history says {', '.join(statuses)}: not executed" if set(statuses) & ORDER_NOT_EXECUTED_STATUSES else None
    fills = [f for f, _, _ in trades.values()]
    wallets = {f.wallet_impact.currency for f in fills}
    if len(wallets) > 1:
        return f"its fills are in more than one wallet currency: {sorted(wallets)}"
    instrument_ccy = found[0][0].order.instrument.currency
    qty = sum(abs(f.quantity_shares) for f in fills)
    value = sum(abs(f.quantity_shares) * f.price_instrument_ccy for f in fills)
    conventions = {_fx(f, instrument_ccy) for f in fills}
    convention = conventions.pop() if len(conventions) == 1 else "undetermined"
    per_unit: float | None = None
    if convention == "same_currency":
        per_unit = 1.0
    elif convention in ("wallet_per_instrument", "instrument_per_wallet"):
        wallet = sum(abs(f.quantity_shares) * f.price_instrument_ccy * (f.wallet_impact.fx_rate if convention == "wallet_per_instrument" else 1.0 / f.wallet_impact.fx_rate)
                     for f in fills)
        per_unit = wallet / value
    (wallet_ccy,) = wallets
    fees: dict[str, float] = {}
    other: list[str] = []
    for f in fills:
        for t in f.wallet_impact.taxes:
            if t.currency == wallet_ccy:
                fees[t.name] = fees.get(t.name, 0.0) + abs(t.amount)
            else:
                other.append(f"{t.name} {abs(t.amount):g} {t.currency}")
    shas = list(dict.fromkeys(sha for _, sha, _ in trades.values()))
    return Execution(source="history_fill", evidence=tuple(shas), fill_ids=tuple(sorted(trades)), filled_shares=qty if s.order.side == "BUY" else -qty,
                     price_instrument_ccy=value / qty, instrument_currency=instrument_ccy, filled_at_utc=max(f.filled_at_utc for f in fills),
                     observed_utc=max(r for _, _, r in trades.values()), wallet_currency=wallet_ccy,
                     wallet_value_wallet_ccy=sum(abs(f.wallet_impact.net_value_wallet_ccy) for f in fills),
                     fx_rates_reported=tuple(f.wallet_impact.fx_rate for f in fills), fx_convention=convention, wallet_per_instrument=per_unit,
                     fees_wallet_ccy=fees, fees_other_ccy=tuple(other))


def _position(snap: Snapshot[tuple[Position, ...]], ticker: str) -> Position | None:
    found = [p for p in snap.data if p.ticker == ticker]
    if len(found) > 1:
        raise FillsInputError(f"positions read {snap.record_sha256[:12]} lists {ticker} twice")
    return found[0] if found else None


def _from_positions(s: SentOrder, b: Batch, before_utc: dt.datetime | None, positions: Sequence[Snapshot[tuple[Position, ...]]],
                    tol: float) -> Execution | str:
    """The execution a positions read implies for the buy `s` of batch `b`, read before `before_utc` (the next batch trading its ticker); else the reason."""
    t, q = s.order.ticker, s.order.quantity_shares
    if s.order.side != "BUY":
        return "a sale does not show in positions: it needs the order history (INTEGRATION_SPEC.md)"
    if b.held_before.get(t, 0.0) > tol:
        return "the position existed before the batch: its average price mixes earlier shares"
    reads = [p for p in positions if p.received_utc > s.sent_utc and (before_utc is None or p.received_utc < before_utc)]
    if not reads:
        return "no full positions read after the order" + (" and before the next batch trading the ticker" if before_utc else "")
    seen: list[float] = []
    for snap in reads:
        pos = _position(snap, t)
        if pos is None:
            seen.append(0.0)
            continue
        if abs(pos.quantity_shares - q) <= tol and pos.created_at_utc < b.started_utc:
            return f"the position at the order's quantity was opened {pos.created_at_utc.isoformat()}, before the batch started: not this order's fill"
        if abs(pos.quantity_shares - q) <= tol:
            w = pos.wallet_impact
            same = w.currency == pos.instrument.currency
            value = pos.quantity_shares * pos.average_price_paid_instrument_ccy
            return Execution(source="position_snapshot", evidence=(snap.record_sha256,), fill_ids=(), filled_shares=pos.quantity_shares,
                             price_instrument_ccy=pos.average_price_paid_instrument_ccy, instrument_currency=pos.instrument.currency,
                             filled_at_utc=pos.created_at_utc, observed_utc=snap.received_utc, wallet_currency=w.currency,
                             wallet_value_wallet_ccy=w.total_cost_wallet_ccy, fx_rates_reported=(), fx_convention="same_currency" if same else "implied",
                             wallet_per_instrument=1.0 if same else (w.total_cost_wallet_ccy / value if value > 0 else None), fees_wallet_ccy=None, fees_other_ccy=())
        seen.append(pos.quantity_shares)
    return (f"the {len(reads)} positions read(s) after the order show {', '.join(f'{x:g}' for x in seen)} share(s), not the order's {q:g}: not filled yet, "
            "partly filled, or changed")


def _ledger_view(state: EngineState, plans: Mapping[str, Plan], measured: Mapping[int, Execution],
                 tol: float) -> tuple[dict[str, dict[str, float]], tuple[tuple[str, str], ...], tuple[LedgerDifference, ...]]:
    view = {str(t): dict(h) for t, h in state.ledger.items()}
    notes: list[tuple[str, str]] = []
    diffs: list[LedgerDifference] = []
    for b in state.batches:
        if b.settled:
            continue
        if b.tranche is None:
            notes.append((b.plan_hash, "names no tranche: not placed in the ledger"))
            continue
        if missing := [s for s in b.sent if s.audit_line not in measured]:
            notes.append((b.plan_hash, f"{len(missing)} accepted order(s) unmeasured: tranche {b.tranche} left as recorded"))
            continue
        realised, expected = dict(plans[b.plan_hash].held_shares), dict(plans[b.plan_hash].held_shares)
        for s in b.sent:
            t = s.order.ticker
            realised[t] = round(realised.get(t, 0.0) + measured[s.audit_line].filled_shares, QTY_DECIMALS)
            expected[t] = round(expected.get(t, 0.0) + s.order.quantity_shares, QTY_DECIMALS)
        view[str(b.tranche)] = {t: q for t, q in sorted(realised.items()) if q}
        notes.append((b.plan_hash, f"tranche {b.tranche} set to the holdings its plan started from plus the shares its fills realised"))
        diffs += [LedgerDifference(tranche=b.tranche, ticker=t, expected_shares=expected.get(t, 0.0), realised_shares=realised.get(t, 0.0))
                  for t in sorted(set(expected) | set(realised)) if abs(realised.get(t, 0.0) - expected.get(t, 0.0)) > tol]
    return view, tuple(notes), tuple(diffs)


def analyse(state: EngineState, plans: Mapping[str, Plan], positions: Sequence[Snapshot[tuple[Position, ...]]],
            history: Sequence[Snapshot[tuple[HistoricalOrder, ...]]], now_utc: dt.datetime, tol: float = QTY_TOL) -> FillReport:
    """The fill and cost report of every accepted order in the engine's records (module docstring). Pure: no file, clock or network is read. `positions` are
    full positions reads and `history` order-history pages, each with its journal record."""
    if now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    if missing := [b.plan_hash[:12] for b in state.batches if b.plan_hash not in plans]:
        raise FillsInputError(f"plan(s) {missing} of the audit log's batches were not given")
    positions = sorted(positions, key=lambda p: p.received_utc)
    by_order: dict[int, list[tuple[HistoricalOrder, str, dt.datetime]]] = {}
    for page in history:
        for h in page.data:
            by_order.setdefault(h.order.order_id, []).append((h, page.record_sha256, page.received_utc))
    costs: list[OrderCost] = []
    unmeasured: list[Unmeasured] = []
    measured: dict[int, Execution] = {}
    for i, b in enumerate(state.batches):
        plan = plans[b.plan_hash]
        for s in b.sent:
            order_id = _answer_id(s)
            got: Execution | str | None = _from_history(s, by_order[order_id]) if order_id in by_order else None
            if got is None:
                nxt = [later.started_utc for later in state.batches[i + 1:] if s.order.ticker in later.tried_tickers]
                got = _from_positions(s, b, nxt[0] if nxt else None, positions, tol)
            if isinstance(got, str):
                unmeasured.append(Unmeasured(plan_hash=b.plan_hash, tranche=b.tranche, audit_line=s.audit_line, ticker=s.order.ticker, side=s.order.side,
                                             planned_shares=s.order.quantity_shares, reason=got))
                continue
            measured[s.audit_line] = got
            costs.append(OrderCost(plan_hash=b.plan_hash, tranche=b.tranche, audit_line=s.audit_line, sent_utc=s.sent_utc, ticker=s.order.ticker, side=s.order.side,
                                   planned_shares=s.order.quantity_shares, plan_notional_usd=s.order.notional_usd, plan_price_usd=s.order.plan_price_usd,
                                   plan_price_bound_bp=s.order.plan_price_bound_bp, plan_price_date=plan.price_date, execution=got))
    view, notes, diffs = _ledger_view(state, plans, measured, tol)
    sources = {"audit_records": state.audit_records, "audit_head": state.audit_head, "ledger_sha256": state.ledger_sha256, "plans": sorted(plans),
               "positions_reads": [p.record_sha256 for p in positions], "history_pages": [h.record_sha256 for h in history]}
    return FillReport(generated_utc=now_utc, costs=tuple(costs), unmeasured=tuple(unmeasured),
                      orders_not_executed=sum(len(plans[b.plan_hash].orders) - len(b.sent) for b in state.batches), ledger_view=view, ledger_batches=notes,
                      ledger_differences=diffs, tolerance_shares=tol, sources=sources)


def main(argv: list[str] | None = None, now: dt.datetime | None = None, out_dir: Path | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quant.execution.brokers.t212.fills", description=__doc__.split("\n")[0])
    ap.add_argument("--journal", type=Path, default=journal.PATH)
    a = ap.parse_args(argv)
    try:
        state = read_engine_state()
        if not state.batches:
            raise FillsInputError("the audit log records no batch: nothing to analyse")
        plans = {b.plan_hash: load_plan(b.plan_hash) for b in state.batches}
        positions, history = snapshots.all_positions(a.journal), snapshots.history_pages(a.journal)
        if not positions and not history:
            raise FillsInputError(f"no positions or order-history read in {a.journal}: run python -m quant.execution.brokers.t212.pull")
        report = analyse(state, plans, positions, history, now or dt.datetime.now(dt.UTC))
        newest = max([p.received_utc for p in positions] + [h.received_utc for h in history])
        json_path, _ = reports.write(f"fills_{reports.stamp(newest)}", report.to_json(), report.to_markdown(), out_dir or reports.REPORTS)
    except (EngineRecordError, FillsInputError, journal.JournalError, snapshots.SnapshotError, FileExistsError) as e:
        print(f"REFUSED: {e}")
        return 2
    print(report.to_markdown())
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
