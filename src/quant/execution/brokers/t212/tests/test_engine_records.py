"""quant.execution.brokers.t212.engine_records: the engine's audit log, plans and ledger read as typed records, read-only. The records are written by the engine itself
(factories.Engine: quant.portfolio.plan.write_plan, quant.execution.engine.send and reconcile_sent) against a broker replaced at urlopen; files live in tmp_path."""
from __future__ import annotations

import json

import pytest

from quant.execution import audit as A
from quant.execution import ledger as LG
from quant.portfolio import plan as P
from quant.execution.brokers.t212 import engine_records as ER
from quant.execution.brokers.t212.tests import factories as F

PRICES = {"AAA_US_EQ": 50.0, "BBB_US_EQ": 20.0, "CCC_US_EQ": 10.0}


def test_a_sent_batch_is_read_with_its_start_positions_answers_and_failures(engine):
    engine.answers["CCC_US_EQ"] = OSError("connection reset")
    p = engine.plan(0, {"AAA_US_EQ": 10.0, "BBB_US_EQ": 0.0, "CCC_US_EQ": 7.0}, PRICES)
    engine.send(p)
    state = ER.read_engine_state()
    (b,) = state.batches
    assert (b.plan_hash, b.tranche, b.planned_orders, b.finished, b.halted, b.not_attempted, b.settled) == (p["plan_hash"], 0, 3, True, False, 0, False)
    assert b.held_before == {"BBB_US_EQ": 4.0} and b.planned_utc <= b.started_utc and b.started_utc.tzinfo is not None
    assert [(s.order.ticker, s.order.quantity_shares, s.response["status"]) for s in b.sent] == [("AAA_US_EQ", 10.0, "NEW"), ("BBB_US_EQ", -4.0, "NEW")]
    assert [(f.order.ticker, "connection reset" in f.error) for f in b.failed] == [("CCC_US_EQ", True)]
    assert b.tried_tickers == {"AAA_US_EQ", "BBB_US_EQ", "CCC_US_EQ"}                 # accepted and failed alike
    assert state.audit_records == len(A.lines()) and state.audit_head == A.verify()["head"] and state.ledger == {0: {"BBB_US_EQ": 4.0}}


def test_expected_holdings_add_the_accepted_orders_of_an_unsettled_batch_to_the_ledger(engine):
    engine.answers["CCC_US_EQ"] = OSError("connection reset")
    p = engine.plan(0, {"AAA_US_EQ": 10.0, "BBB_US_EQ": 0.0, "CCC_US_EQ": 7.0}, PRICES)
    engine.send(p)
    exp = ER.expected_holdings(ER.read_engine_state())
    assert {t: e.quantity_shares for t, e in exp.items()} == {"AAA_US_EQ": 10.0, "BBB_US_EQ": 0.0}           # the failed CCC order is not expected
    assert exp["BBB_US_EQ"].sources[0] == "ledger tranche 0" and exp["AAA_US_EQ"].sources[0].startswith(f"plan {p['plan_hash'][:12]}: order accepted")


def test_a_settled_batch_is_counted_once_through_the_ledger_the_engine_updated(engine):
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    engine.send(p)
    engine.positions = [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0)]
    assert engine.reconcile(p) == []
    state = ER.read_engine_state()
    assert state.batches[0].settled and {t: e.quantity_shares for t, e in ER.expected_holdings(state).items()} == {"AAA_US_EQ": 2.0, "BBB_US_EQ": 4.0}
    assert state.ledger_sha256 is not None and state.ledger == {0: {"BBB_US_EQ": 4.0}, 1: {"AAA_US_EQ": 2.0}}


def test_a_batch_stopped_by_the_kill_switch_is_read_as_halted(engine):
    engine.kill_after = 1
    p = engine.plan(1, {"AAA_US_EQ": 2.0, "CCC_US_EQ": 3.0}, PRICES)
    engine.send(p)
    (b,) = ER.read_engine_state().batches
    assert b.halted and b.finished and len(b.sent) == 1 and b.not_attempted == 1


def test_an_audit_log_with_a_broken_chain_is_not_read(engine):
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES))
    first, *rest = A.PATH.read_bytes().splitlines()
    A.PATH.write_bytes(b"\n".join([first.replace(b"plan", b"plam", 1), *rest]) + b"\n")
    with pytest.raises(ER.EngineRecordError, match=r"audit log chain broken at line\(s\) \[2\]"):
        ER.read_engine_state()


@pytest.mark.parametrize("event,fields,message", [
    ("mystery", {"plan_hash": "x"}, r"audit line 1\.event: 'mystery' is not a documented value"),
    ("send_started", {"plan_hash": "x", "token": "t", "orders": 1}, r"audit line 1\.held_before: required field is absent"),
    ("sent", {"plan_hash": "x", "order": {"ticker": "A", "quantity": 1, "usd": 5, "side": "BUY"}, "response": {}}, "sent for plan x, which was never started"),
    ("reconciled", {"plan_hash": "x", "differences": 0, "note": "?"}, r"undocumented field\(s\) \['note'\]"),
    ("plan", {"plan_hash": "x", "file": "f", "orders": 1, "slot": "27/10/2026", "tranche": None}, r"audit line 1\.slot: not an ISO 8601 date: '27/10/2026'"),
    ("plan", {"plan_hash": "x", "file": "f", "orders": 1, "slot": None, "tranche": "0"}, r"audit line 1\.tranche: expected integer, got string"),
    ("send_started", {"plan_hash": "x", "token": "t", "orders": 1, "held_before": {}}, "audit line 1: plan x started without a plan record"),
])
def test_records_the_engine_does_not_write_are_refused_with_their_line(engine, event, fields, message):
    A.log(event, **fields)
    with pytest.raises(ER.EngineRecordError, match=message):
        ER.read_engine_state()


def test_a_refusal_in_the_audit_log_is_read_and_starts_no_batch(engine):
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    P.write_plan(p)
    with pytest.raises(engine.E.Refused, match="no owner confirmation"):
        engine.E.send(p, True, sleep=0)                                    # no token: the engine records its refusal
    state = ER.read_engine_state()
    assert [r["event"] for r in A.records()] == ["plan", "refused"] and state.batches == () and state.audit_records == 2


def test_a_pre_a6_plan_record_takes_its_tranche_from_its_slot_as_the_engine_does(engine):
    A.log("plan", plan_hash="x" * 64, file="f", orders=1, slot="2026-10-27", tranche=None)
    A.log("send_started", plan_hash="x" * 64, token="t", orders=1, held_before={})
    (b,) = ER.read_engine_state().batches
    assert b.tranche == engine.E.tranche_of_plan({"slot": "2026-10-27"}) is not None


def test_a_plan_record_without_file_slot_or_tranche_is_read_with_no_tranche(engine):
    A.log("plan", plan_hash="x" * 64, orders=1)
    A.log("send_started", plan_hash="x" * 64, token="t", orders=1, held_before={})
    (b,) = ER.read_engine_state().batches
    assert b.tranche is None and b.planned_orders == 1


def test_a_reconciliation_with_differences_leaves_its_batch_unsettled(engine):
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    engine.send(p)
    assert engine.reconcile(p) != []                                       # the broker still shows no AAA
    (b,) = ER.read_engine_state().batches
    assert not b.settled and [r["differences"] for r in A.records() if r["event"] == "reconciled"] == [1]


def test_a_finished_batch_whose_counts_contradict_its_orders_is_refused(engine):
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    engine.send(p)
    A.log("send_finished", plan_hash=p["plan_hash"], accepted=5, errors=0, not_attempted=0)
    with pytest.raises(ER.EngineRecordError, match="finished with 5 accepted and 0 failed, but the log records 1 and 0"):
        ER.read_engine_state()


def test_a_batch_started_twice_is_refused(engine):
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    engine.send(p)
    A.log("send_started", plan_hash=p["plan_hash"], token="t", orders=1, held_before={})
    with pytest.raises(ER.EngineRecordError, match="started a second time"):
        ER.read_engine_state()


def test_an_order_record_the_engine_would_not_write_is_refused(engine):
    p = engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES)
    engine.send(p)
    A.log("error", plan_hash=p["plan_hash"], order={"ticker": "AAA_US_EQ", "quantity": 2.0, "usd": 100.0, "side": "SELL"}, error="x")
    with pytest.raises(ER.EngineRecordError, match="side SELL, quantity 2.0"):
        ER.read_engine_state()


class TestPlans:
    def test_a_stored_plan_is_read_with_its_orders_tranche_and_starting_holdings(self, engine):
        p = engine.plan(0, {"AAA_US_EQ": 10.0, "BBB_US_EQ": 0.0}, PRICES)
        P.write_plan(p)
        plan = ER.load_plan(p["plan_hash"])
        assert (plan.tranche, plan.account_value_usd, dict(plan.held_shares), str(plan.slot), str(plan.price_date)) == (0, 10000.0, {"BBB_US_EQ": 4.0}, "2026-10-27", "2026-10-23")
        aaa = next(o for o in plan.orders if o.ticker == "AAA_US_EQ")
        assert (aaa.side, aaa.quantity_shares, aaa.plan_price_usd) == ("BUY", 10.0, 50.0) and aaa.plan_price_bound_bp == pytest.approx(0.1)

    def test_a_plan_made_before_amendment_a6_starts_from_an_empty_tranche(self, engine):
        p = engine.plan(1, {"AAA_US_EQ": 1.0}, PRICES)
        del p["held"]
        P.write_plan(p)
        assert dict(ER.load_plan(p["plan_hash"]).held_shares) == {}

    def test_a_plan_changed_after_it_was_written_is_refused(self, engine):
        p = engine.plan(1, {"AAA_US_EQ": 1.0}, PRICES)
        f = P.write_plan(p)
        doc = json.loads(f.read_text())
        doc["orders"][0]["quantity"] = 100.0
        f.write_text(json.dumps(doc))
        with pytest.raises(ER.EngineRecordError, match="do not hash to its name"):
            ER.load_plan(p["plan_hash"])

    def test_a_missing_plan_or_one_with_an_undocumented_field_is_refused(self, engine):
        with pytest.raises(ER.EngineRecordError, match="no file"):
            ER.load_plan("0" * 64)
        p = engine.plan(1, {"AAA_US_EQ": 1.0}, PRICES)
        P.write_plan({**p, "comment": "edited"})
        with pytest.raises(ER.EngineRecordError, match=r"undocumented field\(s\) \['comment'\]"):
            ER.load_plan(p["plan_hash"])


def test_a_ledger_with_a_key_that_is_not_a_tranche_is_refused(engine):
    LG.LEDGER.write_text(json.dumps({"zero": {"AAA_US_EQ": 1.0}}))
    with pytest.raises(ER.EngineRecordError, match="tranche key 'zero' is not a tranche number"):
        ER.read_ledger()
    LG.LEDGER.write_text(json.dumps({"0": {"AAA_US_EQ": "1"}}))
    with pytest.raises(ER.EngineRecordError, match=r"ledger\.0\.AAA_US_EQ: expected number"):
        ER.read_ledger()


def test_no_ledger_yet_reads_as_empty_without_a_hash(engine):
    LG.LEDGER.unlink()
    assert ER.read_ledger() == ({}, None)
