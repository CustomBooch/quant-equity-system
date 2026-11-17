"""quant.execution.brokers.t212.reconcile: the practice account against the engine's records. The records are written by the engine itself (factories.Engine: plans written,
confirmed, sent through every gate and reconciled by quant.execution.engine against a broker replaced at urlopen); the broker's state is parsed from response bodies
in the documented shapes; the journal and the reports are real files in tmp_path. Times are taken from the batch the engine recorded: seven days after it
is always five business days, past the two-day window; the same day is inside it."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random

import pytest

from quant.execution import audit as A
from quant.execution import ledger as LG
from quant.execution.brokers.t212 import client as C
from quant.execution.brokers.t212 import engine_records as ER
from quant.execution.brokers.t212 import journal as J
from quant.execution.brokers.t212 import reconcile as RC
from quant.execution.brokers.t212 import reports
from quant.execution.brokers.t212.records import parse_account_summary, parse_orders, parse_positions
from quant.execution.brokers.t212.tests import factories as F

PRICES = {"AAA_US_EQ": 50.0, "BBB_US_EQ": 20.0, "CCC_US_EQ": 10.0}
PAST = dt.timedelta(days=7)                                  # five business days after the batch
SOURCES = {"account_summary": ("a" * 64,), "positions": ("b" * 64,)}


def broker(positions, summary=None, pending=None):
    return RC.BrokerState(as_of_utc=F.T0, summary=parse_account_summary(summary or F.account_summary()), positions=parse_positions(positions),
                          pending_orders=None if pending is None else parse_orders(pending), sources=SOURCES)


def run(positions, after=PAST, summary=None, pending=None, tol=None):
    """Reconcile the engine's records against `positions`, `after` the newest batch started."""
    state = ER.read_engine_state()
    plans = {b.plan_hash: ER.load_plan(b.plan_hash) for b in state.batches if not b.settled}
    return RC.reconcile(state, plans, broker(positions, summary, pending), state.batches[-1].started_utc + after, tol)


def buy_two_aaa(engine, **answers):
    """Tranche 1 buys 2 AAA (100 USD at the plan's 50) through the engine; `answers` replaces the broker's answer to a ticker's order."""
    engine.answers.update(answers)
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    engine.send(p)
    return p


def test_a_batch_that_filled_as_planned_reconciles_clean_with_every_position_matched(engine):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.1)])
    assert r.verdict == "clean" and r.breaks == 0 and [(p.ticker, p.kind) for p in r.positions] == [("AAA_US_EQ", "matched"), ("BBB_US_EQ", "matched")]
    (c,) = r.price_checks
    assert (c.ticker, c.status, round(c.deviation_bp, 6), c.plan_price_usd) == ("AAA_US_EQ", "ok", 20.0, 50.0)
    assert r.positions[1].sources == ("ledger tranche 0",) and r.rejection_rate == 0.0 and r.orders_counted_for_rate == 1 and not r.intent_gaps


def test_a_difference_inside_the_settlement_window_awaits_settlement_and_is_not_a_break(engine):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0)], after=dt.timedelta(0))
    assert r.verdict == "clean" and [(p.ticker, p.kind, p.difference_shares) for p in r.positions] == [("AAA_US_EQ", "awaiting_settlement", -2.0),
                                                                                                         ("BBB_US_EQ", "matched", 0.0)]


@pytest.mark.parametrize("held,kind", [(None, "missing"), (1.0, "short"), (3.0, "excess"), (-1.0, "negative")])
def test_a_quantity_break_past_the_window_is_named_by_what_the_broker_holds(engine, held, kind):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0)] + ([] if held is None else [F.position("AAA_US_EQ", held)]))
    (q,) = r.quantity_breaks()
    assert (q.ticker, q.kind, q.expected_shares, q.broker_shares) == ("AAA_US_EQ", kind, 2.0, held or 0.0) and r.verdict == "breaks"
    assert q.sources[0].startswith("plan ") and "order accepted, audit line" in q.sources[0]


def test_an_order_answered_not_final_is_inferred_stuck_only_when_its_ticker_breaks_the_way_a_missing_fill_would(engine):
    buy_two_aaa(engine)
    short, excess = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 1.0)]), run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 3.0)])
    (s,) = short.orders_stuck()
    assert (s.kind, s.ticker, s.status, s.age_business_days) == ("stuck_inferred", "AAA_US_EQ", "NEW", 5) and "pending orders are not read" in s.detail
    assert short.quantity_breaks()[0].orders == ("stuck_inferred",) and excess.orders_stuck() == () and excess.breaks == 1


def test_a_position_the_records_do_not_explain_is_an_unknown_position_and_a_zero_one_is_ignored(engine):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0), F.position("ZZZ_US_EQ", 5.0, 12.0), F.position("YYY_US_EQ", 0.0)])
    (u,) = r.unknown_positions
    assert (u.ticker, u.broker_shares, u.average_price_instrument_ccy, u.instrument_currency) == ("ZZZ_US_EQ", 5.0, 12.0, "USD") and r.breaks == 1


def test_a_ticker_every_tranche_sold_and_the_broker_no_longer_holds_has_no_line(engine):
    engine.send(engine.plan(0, {}, PRICES))                             # tranche 0 sells its 4 BBB
    r = run([])
    assert [p.ticker for p in r.positions] == [] and r.verdict == "clean" and r.price_checks[0].status == "unchecked"


@pytest.mark.parametrize("position,reason,deviation", [
    (F.position("AAA_US_EQ", 2.0, 5010.0), "average price paid against the plan price, tolerance 1000 bp", 992000.0),  # pence against dollars: 100x
    (F.position("AAA_US_EQ", 2.0, 50.0, currency="GBX"), "held in GBX; the plan's price is USD", None),
])
def test_a_price_far_from_the_plan_or_in_another_currency_is_a_price_break(engine, position, reason, deviation):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0), position])
    (c,) = r.price_breaks()
    assert (c.ticker, c.reason) == ("AAA_US_EQ", reason) and (c.deviation_bp is None if deviation is None else round(c.deviation_bp, 6) == deviation)


def test_a_price_is_not_checked_when_the_average_mixes_earlier_shares_a_sale_or_nothing_held(engine):
    engine.positions = [F.position("BBB_US_EQ", 4.0)]
    p = engine.plan(1, {"BBB_US_EQ": 3.0, "AAA_US_EQ": 2.0}, PRICES)        # tranche 1 buys BBB, which tranche 0 already holds
    engine.send(p)
    engine.send(engine.plan(0, {}, PRICES))                                 # tranche 0 sells its BBB in another batch
    r = run([F.position("BBB_US_EQ", 3.0)])
    reasons = {c.ticker: c.reason for c in r.price_checks}
    assert {c.status for c in r.price_checks} == {"unchecked"} and reasons["AAA_US_EQ"] == "nothing held to compare (see the quantity breaks)"
    assert reasons["BBB_US_EQ"].startswith("more than one order, or a sale")


def test_a_position_held_before_its_batch_is_not_price_checked(engine):
    engine.send(engine.plan(1, {"BBB_US_EQ": 3.0}, PRICES))
    (c,) = run([F.position("BBB_US_EQ", 7.0, 19.6)]).price_checks
    assert (c.status, c.reason) == ("unchecked", "the position existed before the batch: its average price mixes earlier shares")


@pytest.mark.parametrize("status,kind", [("REJECTED", "rejected_by_broker"), ("CANCELLED", "cancelled_by_broker")])
def test_an_order_the_broker_rejected_or_cancelled_is_reported_with_its_audit_line(engine, status, kind):
    buy_two_aaa(engine, AAA_US_EQ=F.order(777, "AAA_US_EQ", 2.0, status=status))
    r = run([F.position("BBB_US_EQ", 4.0)], after=dt.timedelta(0))
    (o,) = r.orders_rejected()
    sent_line = next(i for i, rec in enumerate(A.records(), start=1) if rec["event"] == "sent")
    assert (o.kind, o.status, o.audit_line, o.detail) == (kind, status, sent_line, f"broker order 777 answered {status}")
    assert r.rejection_rate == 1.0 and r.verdict == "breaks" and r.positions[0].kind == "awaiting_settlement"


def test_a_submission_that_failed_is_rejected_and_its_order_is_an_intent_gap(engine):
    engine.answers["CCC_US_EQ"] = OSError("connection reset")
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0, "CCC_US_EQ": 5.0}, PRICES))
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])
    (o,) = r.orders_rejected()
    assert (o.kind, o.ticker, o.status) == ("failed_at_submission", "CCC_US_EQ", None) and "connection reset" in o.detail
    assert [(g.ticker, g.reason) for g in r.intent_gaps] == [("CCC_US_EQ", "submission failed")] and r.rejection_rate == 0.5
    assert r.to_json()["intent_gaps"] == [{"plan_hash": o.plan_hash, "ticker": "CCC_US_EQ", "quantity_shares": 5.0, "reason": "submission failed"}]


def test_an_order_still_pending_at_the_broker_is_information_in_the_window_and_stuck_after_it(engine):
    buy_two_aaa(engine)
    pending = [F.order(501, "AAA_US_EQ", 2.0)]                               # the harness answers the first order with id 501
    inside, past = run([F.position("BBB_US_EQ", 4.0)], after=dt.timedelta(0), pending=pending), run([F.position("BBB_US_EQ", 4.0)], pending=pending)
    assert [o.kind for o in inside.order_issues] == ["pending_within_window"] and inside.verdict == "clean" and inside.pending_orders_read
    assert [o.kind for o in past.orders_stuck()] == ["stuck_pending_at_broker"] and past.to_json()["orders_pending"] == []
    reserved = run([F.position("BBB_US_EQ", 4.0)], after=dt.timedelta(0), pending=pending, summary=F.account_summary(reserved=100.0))
    assert reserved.cash == () and reserved.verdict == "clean"                  # cash reserved for an order that is pending


def test_an_order_not_pending_once_pending_orders_are_read_is_not_inferred_stuck(engine):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0)], pending=[])
    assert r.orders_stuck() == () and [p.kind for p in r.quantity_breaks()] == ["missing"]


def test_an_answer_that_is_not_a_documented_order_is_unparseable_and_names_its_audit_line(engine):
    buy_two_aaa(engine, AAA_US_EQ={"id": 9})
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])
    (u,) = r.unparseable_answers
    assert u.ticker == "AAA_US_EQ" and u.error.startswith(f"audit line {u.audit_line}.response.") and "required field is absent" in u.error
    assert r.breaks == 1 and r.rejection_rate == 0.0
    assert r.to_json()["unparseable_answers"] == [{"plan_hash": u.plan_hash, "ticker": "AAA_US_EQ", "audit_line": u.audit_line, "error": u.error}]


def test_an_order_the_broker_answered_filled_raises_no_order_issue_even_beside_a_break(engine):
    buy_two_aaa(engine, AAA_US_EQ=F.order(501, "AAA_US_EQ", 2.0, status="FILLED", filled=2.0))
    r = run([F.position("BBB_US_EQ", 4.0)])
    assert r.order_issues == () and [p.kind for p in r.quantity_breaks()] == ["missing"]


def test_with_no_batch_the_ledger_alone_is_reconciled(engine):
    state = ER.read_engine_state()
    r = RC.reconcile(state, {}, broker([F.position("BBB_US_EQ", 4.0)]), F.T0)
    assert r.verdict == "clean" and r.batches == () and r.rejection_rate is None and "## Batches" not in r.to_markdown()
    assert "Rejection rate over all batches: none sent" in r.to_markdown()


@pytest.mark.parametrize("summary,pending,checks", [
    (F.account_summary(currency="USD"), None, ["account_currency"]),
    (F.account_summary(available=-5.0), None, ["negative_cash"]),
    (F.account_summary(reserved=10.0), [], ["reserved_without_pending"]),
])
def test_cash_breaks_name_the_currency_negative_cash_and_cash_reserved_with_no_order_pending(engine, summary, pending, checks):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)], summary=summary, pending=pending)
    assert [c.check for c in r.cash_breaks()] == checks and r.verdict == "breaks"


def test_cash_reserved_while_pending_orders_are_not_read_is_a_note_not_a_break(engine):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)], summary=F.account_summary(reserved=10.0))
    assert r.verdict == "clean" and r.to_json()["information"]["cash_notes"] == [{"check": "reserved_cash",
                                                                                  "detail": "10.00 GBP reserved for orders; pending orders are not read"}]


def test_the_broker_s_own_numbers_are_checked_for_consistency_as_information(engine):
    buy_two_aaa(engine)
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)], summary=F.account_summary(total=10000.0, available=2400.0, invested=7500.0))
    assert round(r.total_value_gap_account_ccy, 6) == 100.0 and r.verdict == "clean"
    assert round(r.positions_value_gap_account_ccy, 6) == 2 * 408.0 - 7500.0             # each position's wallet value is 400 x 51 / 50


def test_orders_not_attempted_after_the_kill_switch_are_intent_gaps(engine):
    engine.kill_after = 1
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0, "CCC_US_EQ": 3.0}, PRICES))
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])
    assert [(g.ticker, g.reason) for g in r.intent_gaps] == [("CCC_US_EQ", "not attempted: stopped by the kill switch")] and r.batches[0].halted


def test_a_settled_batch_is_reconciled_through_the_ledger_and_its_orders_are_no_longer_open(engine):
    p = buy_two_aaa(engine, AAA_US_EQ=F.order(1, "AAA_US_EQ", 2.0, status="REJECTED"))
    engine.positions = [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0)]
    assert engine.reconcile(p) == []
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])
    assert r.verdict == "clean" and r.order_issues == () and r.price_checks == () and r.batches[0].settled and r.rejection_rate == 1.0


def test_inputs_that_cannot_be_reconciled_are_refused(engine):
    buy_two_aaa(engine)
    state = ER.read_engine_state()
    (b,) = state.batches
    plan = ER.load_plan(b.plan_hash)
    held = broker([F.position("AAA_US_EQ", 2.0)])
    with pytest.raises(RC.ReconcileInputError, match="the broker lists AAA_US_EQ twice"):
        RC.reconcile(state, {b.plan_hash: plan}, broker([F.position("AAA_US_EQ", 1.0), F.position("AAA_US_EQ", 1.0)]), b.started_utc)
    with pytest.raises(RC.ReconcileInputError, match=f"plan {b.plan_hash[:12]} of an open batch was not given"):
        RC.reconcile(state, {}, held, b.started_utc)
    extra = dataclasses.replace(plan, orders=(*plan.orders, ER.PlannedOrder(ticker="CCC_US_EQ", quantity_shares=1.0, notional_usd=10.0, side="BUY")))
    with pytest.raises(RC.ReconcileInputError, match="the audit log says 0 order\\(s\\) not attempted, the plan leaves 1"):
        RC.reconcile(state, {b.plan_hash: extra}, held, b.started_utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        RC.reconcile(state, {b.plan_hash: plan}, held, b.started_utc.replace(tzinfo=None))


def test_a_batch_without_its_finish_record_leaves_every_unattempted_order_as_an_intent_gap(engine):
    buy_two_aaa(engine)
    state = ER.read_engine_state()
    (b,) = state.batches
    unfinished = dataclasses.replace(state, batches=(dataclasses.replace(b, finished=False, not_attempted=None, sent=()),))
    r = RC.reconcile(unfinished, {b.plan_hash: ER.load_plan(b.plan_hash)}, broker([F.position("BBB_US_EQ", 4.0)]), b.started_utc + PAST)
    assert [(g.ticker, g.reason) for g in r.intent_gaps] == [("AAA_US_EQ", "the batch did not finish (no send_finished record)")]


def test_the_same_inputs_give_the_same_report_bytes_and_numbers_are_rounded(engine):
    buy_two_aaa(engine, AAA_US_EQ=F.order(777, "AAA_US_EQ", 2.0, status="REJECTED"))
    positions = [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 1.00000004, 50.123456789), F.position("ZZZ_US_EQ", 5.0)]
    one, two = run(positions), run(positions)
    assert reports.dumps(one.to_json()) == reports.dumps(two.to_json()) and one.to_markdown() == two.to_markdown()
    j = one.to_json()
    assert j["verdict"] == "breaks" and j["counts"]["quantity_breaks"] == 1 and j["counts"]["unknown_positions"] == 1 and j["counts"]["orders_rejected"] == 1
    assert j["quantity_breaks"][0]["broker_shares"] == 1.0 and j["quantity_breaks"][0]["difference_shares"] == -1.0
    assert j["price_checks"][0]["broker_average_price_instrument_ccy"] == 50.1235 and j["information"]["rejection_rate"] == 1.0
    assert j["sources"]["journal"] == {k: list(v) for k, v in SOURCES.items()} and j["sources"]["audit_head"] == A.verify()["head"]


def test_a_negative_zero_is_written_as_zero():
    line = RC.PositionLine(ticker="A", kind="matched", expected_shares=-0.00001, broker_shares=0.0, sources=(), orders=())
    assert json.dumps(line.to_json()["expected_shares"]) == "0.0" and line.difference_shares == 0.0


def test_the_markdown_summary_states_the_verdict_and_lists_each_break(engine):
    buy_two_aaa(engine, AAA_US_EQ=F.order(777, "AAA_US_EQ", 2.0, status="REJECTED"))
    md = run([F.position("BBB_US_EQ", 4.0), F.position("ZZZ_US_EQ", 5.0, 12.0)], summary=F.account_summary(available=-5.0)).to_markdown()
    assert md.startswith("# Trading 212 reconciliation\n") and "**Verdict: breaks (4).**" in md and "pending orders read: no" in md
    assert "| quantity: missing | AAA_US_EQ | expected 2, broker 0 (-2); from plan " in md and "| unknown position | ZZZ_US_EQ | 5 shares at 12 USD |" in md
    assert "| order: rejected_by_broker | AAA_US_EQ |" in md and "| cash: negative_cash |  | available to trade -5.00 GBP |" in md
    assert "Rejection rate over all batches: 100.00% of 1 order(s)" in md and md.endswith("GBP.\n")


def test_a_long_list_of_breaks_is_cut_in_the_markdown_and_kept_whole_in_the_json(engine):
    buy_two_aaa(engine)
    many = [F.position(f"Z{i:02d}_US_EQ", 1.0) for i in range(RC.MAX_TABLE_ROWS + 5)]
    r = run([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0), *many])
    assert len(r.to_json()["unknown_positions"]) == RC.MAX_TABLE_ROWS + 5 and "... and 5 more in the JSON report." in r.to_markdown()


@pytest.mark.parametrize("start,end,days", [
    ("2026-10-23T20:00:00+00:00", "2026-10-26T09:00:00+00:00", 1),        # Friday to Monday
    ("2026-10-23T20:00:00+00:00", "2026-10-25T09:00:00+00:00", 0),        # Friday to Sunday
    ("2026-10-27T09:00:00+00:00", "2026-11-03T09:00:00+00:00", 5),        # a week
    ("2026-10-27T09:00:00+00:00", "2026-10-27T23:00:00+00:00", 0),        # the same day
    ("2026-10-27T23:30:00-05:00", "2026-10-28T06:00:00+00:00", 0),        # UTC dates: both are 28 October
])
def test_business_days_are_the_weekdays_after_the_start_date_up_to_the_end_date_in_utc(start, end, days):
    assert RC.business_days_after(dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)) == days


def journal_with(path, positions, summary=None, positions_minutes=0, summary_minutes=1):
    for endpoint, read_path, value, minutes in (("positions", "/equity/positions", positions, positions_minutes),
                                                ("account_summary", "/equity/account/summary", summary or F.account_summary(), summary_minutes)):
        J.append(endpoint=endpoint, path=read_path, pull=f"{endpoint}@{minutes}", page=0, status=200, body=F.body(value), rate_limit=None,
                 received_utc=F.T0 + dt.timedelta(minutes=minutes), journal=path)


def test_the_command_reconciles_the_newest_journaled_reads_and_writes_both_reports(engine, tmp_path, capsys):
    buy_two_aaa(engine)
    journal_with(tmp_path / "j.jsonl", [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])
    now = ER.read_engine_state().batches[0].started_utc + PAST
    assert RC.main(["--journal", str(tmp_path / "j.jsonl")], now=now, out_dir=tmp_path / "out") == 0
    stem = tmp_path / "out" / "reconcile_20261027T143100Z"
    payload = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
    entries = J.entries(tmp_path / "j.jsonl")
    assert payload["verdict"] == "clean" and payload["sources"]["journal"] == {"account_summary": [entries[1].record_sha256], "positions": [entries[0].record_sha256]}
    out = capsys.readouterr().out
    assert stem.with_suffix(".md").read_text(encoding="utf-8") in out and out.endswith(f"{stem.with_suffix('.json')}\n")


def test_the_command_leaves_the_engine_s_records_byte_for_byte_as_it_found_them(engine, tmp_path):
    buy_two_aaa(engine)
    journal_with(tmp_path / "j.jsonl", [F.position("BBB_US_EQ", 4.0)])                 # a break: nothing is recorded either way
    records = [A.PATH, LG.LEDGER, *sorted(engine.P.PLANS.iterdir())]
    before = {p: p.read_bytes() for p in records}
    now = ER.read_engine_state().batches[0].started_utc + PAST
    assert RC.main(["--journal", str(tmp_path / "j.jsonl")], now=now, out_dir=tmp_path / "out") == 1
    assert {p: p.read_bytes() for p in records} == before and sorted(engine.P.PLANS.iterdir()) == records[2:]


def test_the_command_exits_1_when_there_are_breaks_and_takes_its_tolerances_from_the_arguments(engine, tmp_path):
    buy_two_aaa(engine)
    journal_with(tmp_path / "j.jsonl", [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 60.0)])
    now = ER.read_engine_state().batches[0].started_utc + PAST
    assert RC.main(["--journal", str(tmp_path / "j.jsonl"), "--price-tol-bp", "100", "--settle-days", "3", "--currency", "GBP"], now=now,
                   out_dir=tmp_path / "out") == 1
    payload = json.loads((tmp_path / "out" / "reconcile_20261027T143100Z.json").read_text(encoding="utf-8"))
    assert payload["counts"]["price_breaks"] == 1 and payload["tolerances"]["price_bp"] == 100.0 and payload["tolerances"]["settle_business_days"] == 3


def test_the_live_command_reads_the_account_through_the_journaling_client(engine, tmp_path, broker, clock):
    buy_two_aaa(engine)
    broker.answers += [F.ok(F.account_summary()), F.ok([F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])]
    client = C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(5), journal_path=tmp_path / "j.jsonl")
    now = ER.read_engine_state().batches[0].started_utc + PAST
    assert RC.main(["--live", "--journal", str(tmp_path / "j.jsonl")], now=now, client=client, out_dir=tmp_path / "out") == 0
    assert broker.paths() == ["/equity/account/summary", "/equity/positions"] and len(J.entries(tmp_path / "j.jsonl")) == 2


def test_a_summary_read_alone_later_does_not_displace_the_one_read_with_the_positions(engine, tmp_path):
    buy_two_aaa(engine)
    journal_with(tmp_path / "j.jsonl", [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0, 50.0)])
    J.append(endpoint="account_summary", path="/equity/account/summary", pull="health", page=0, status=200, body=F.body(F.account_summary(currency="USD")),
             rate_limit=None, received_utc=F.T0 + dt.timedelta(hours=3), journal=tmp_path / "j.jsonl")
    b = RC.broker_from_journal(tmp_path / "j.jsonl")
    entries = J.entries(tmp_path / "j.jsonl")
    assert b.summary.account_currency == "GBP" and b.sources["account_summary"] == (entries[1].record_sha256,) and b.as_of_utc == F.T0 + dt.timedelta(minutes=1)


def _no_journal(tmp_path, engine, monkeypatch):
    return ["--journal", str(tmp_path / "none.jsonl")]


def _no_summary(tmp_path, engine, monkeypatch):
    J.append(endpoint="positions", path="/equity/positions", pull="p", page=0, status=200, body=F.body([]), rate_limit=None, received_utc=F.T0,
             journal=tmp_path / "j.jsonl")
    return ["--journal", str(tmp_path / "j.jsonl")]


def _skewed(tmp_path, engine, monkeypatch):
    journal_with(tmp_path / "j.jsonl", [], summary_minutes=16)
    return ["--journal", str(tmp_path / "j.jsonl")]


def _broken_journal(tmp_path, engine, monkeypatch):
    journal_with(tmp_path / "j.jsonl", [])
    first, second = (tmp_path / "j.jsonl").read_bytes().splitlines()
    (tmp_path / "j.jsonl").write_bytes(first.replace(b'"page":0', b'"page":1') + b"\n" + second + b"\n")
    return ["--journal", str(tmp_path / "j.jsonl")]


def _bad_body(tmp_path, engine, monkeypatch):
    journal_with(tmp_path / "j.jsonl", [{**F.position(), "quantity": "2"}])
    return ["--journal", str(tmp_path / "j.jsonl")]


def _broken_audit(tmp_path, engine, monkeypatch):
    first, *rest = A.PATH.read_bytes().splitlines()
    A.PATH.write_bytes(b"\n".join([first.replace(b"plan", b"plam", 1), *rest]) + b"\n")
    return ["--journal", str(tmp_path / "none.jsonl")]


def _report_exists(tmp_path, engine, monkeypatch):
    journal_with(tmp_path / "j.jsonl", [])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "reconcile_20261027T143100Z.md").write_text("kept")
    return ["--journal", str(tmp_path / "j.jsonl")]


def _live_without_keys(tmp_path, engine, monkeypatch):
    monkeypatch.delenv("T212_READ_API_KEY")
    return ["--live", "--journal", str(tmp_path / "j.jsonl")]


def _live_unauthorized(tmp_path, engine, monkeypatch):
    return ["--live", "--journal", str(tmp_path / "j.jsonl")]


@pytest.mark.parametrize("setup,message", [
    (_no_journal, "no positions read in"),
    (_no_summary, "no account summary read in"),
    (_skewed, "the newest positions and the nearest account summary were read 0:16:00 apart"),
    (_broken_journal, "fails verification: broken line(s) [2]"),
    (_bad_body, "quantity: expected number"),
    (_broken_audit, "audit log chain broken"),
    (_report_exists, "reports are never overwritten"),
    (_live_without_keys, "T212_READ_API_KEY or T212_READ_SECRET_KEY is not set"),
    (_live_unauthorized, "HTTP 401"),
])
def test_the_command_refuses_with_exit_2_and_writes_nothing_when_its_inputs_cannot_be_reconciled(engine, tmp_path, monkeypatch, broker, clock, capsys,
                                                                                                    setup, message):
    buy_two_aaa(engine)
    argv = setup(tmp_path, engine, monkeypatch)
    broker.answers += [F.status(401, b'{"code": "unauthorized"}')]
    client = C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(5), journal_path=tmp_path / "j.jsonl")
    capsys.readouterr()
    assert RC.main(argv, now=F.T0 + PAST, client=client, out_dir=tmp_path / "out") == 2
    out = capsys.readouterr().out
    assert out.startswith("REFUSED: ") and message in out
    assert not (tmp_path / "out" / "reconcile_20261027T143100Z.json").exists()
