"""quant.execution.engine: every send gate, the audit trail, reconciliation. Boundary replaced: the broker's HTTP (urlopen); the files go to tmp_path."""
from __future__ import annotations

import datetime as dt
import io
import json
import sys
import types
import urllib.error
import urllib.request

import pandas as pd
import pytest

from quant.execution import audit as A
from quant.execution import engine as E
from quant.portfolio import schedule as SCH
from quant.execution import ledger as LG
from quant.portfolio import plan as P

PRICES = {"AAA": 50.0, "BBB": 20.0}
AFTER_CUTOFF = dt.datetime(2026, 11, 17, 0, 5, tzinfo=dt.timezone.utc)          # Monday 19:05 in New York: queued for Tuesday's opening auction
SLOT = "2026-11-17"                                                              # the Tuesday after AFTER_CUTOFF (Monday evening in New York)
TRADABLE = {"AAA", "BBB", "BRK.B", *(f"T{c}" for c in "ABCDEFGHIJK")}
PAPER = "https://paper-api.alpaca.markets"
THIS_MONTH = dt.date.today().strftime("%Y-%m")                                   # the decision month of the book the sample plans were made from


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    for name in ("CONFIRM", "KILL", "LOCK"):
        monkeypatch.setattr(E, name, tmp_path / name.lower())
    monkeypatch.setattr(P, "PLANS", tmp_path / "plans")
    monkeypatch.setattr(A, "PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(LG, "LEDGER", tmp_path / "ledger.json")
    E.CONFIRM.mkdir()
    LG.record_batch({}, 0, {"BBB": 4.0})                                        # tranche 0 holds the 4 BBB the sample plan sells
    monkeypatch.setattr(E, "SEND_BROKER", "alpaca")
    monkeypatch.setattr(E, "alpaca_tradable", lambda: TRADABLE)                 # the newest asset list, replaced by a fixed set
    monkeypatch.setattr(E, "CLOCK", lambda: AFTER_CUTOFF)                       # a send time Alpaca accepts opening-auction orders at (A13)
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "kid-x1")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "secret-x2")


@pytest.fixture(autouse=True)
def opening_auction_orders(monkeypatch):
    """The tests below were written for amendment A13 (opening-auction orders); A31 made day orders the default, tested in TestDayOrders."""
    monkeypatch.setattr(E, "DAY_ORDERS", False)


@pytest.fixture()
def broker(monkeypatch):
    """The Alpaca paper API behind urlopen. GETs: positions answer `positions` (or raise it), the account answers `equity`, an order looked up by its
    client order id answers `orders[id]` or 404. Each POST is recorded and raises for the symbols in `fail`."""
    state = types.SimpleNamespace(requests=[], fail=set(), positions=[{"symbol": "BBB", "qty": "4", "side": "long", "market_value": "80"}], equity="10000", orders={}, open_orders=[])

    def urlopen(req, timeout):
        if req.get_method() == "GET":
            path = req.full_url.removeprefix(PAPER)
            if path == "/v2/account":
                return io.BytesIO(json.dumps({"equity": state.equity}).encode())
            if path.startswith("/v2/orders?status=open"):
                return io.BytesIO(json.dumps(state.open_orders).encode())
            if path.startswith("/v2/orders:by_client_order_id?client_order_id="):
                cid = path.split("=", 1)[1]
                if cid not in state.orders:
                    raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b"{}"))
                return io.BytesIO(json.dumps(state.orders[cid]).encode())
            if isinstance(state.positions, Exception):
                raise state.positions
            return io.BytesIO(json.dumps(state.positions).encode())
        body = json.loads(req.data)
        state.requests.append((req, body))
        if body["symbol"] in state.fail:
            raise OSError("connection reset")
        return io.BytesIO(json.dumps({"id": f"o{len(state.requests)}", "status": "accepted"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return state


def plan(**kw):
    """Tranche 0 buys 10 AAA from nothing and sells the 4 BBB it holds: 500 and 80 USD on a 10,000 USD account. Not yet recorded."""
    return rehash({**P.make_plan({"AAA": 10.0, "BBB": 0.0}, {"BBB": 4.0}, PRICES, 10000.0, **kw), "tranche": 0, "held": {"BBB": 4.0}, "broker": "alpaca",
                   "decision_month": THIS_MONTH, "slot": SLOT})


def rehash(p):
    """The plan's hash over its orders, account value and tranche, as plan_from_target records it (to-do F6)."""
    return p | {"plan_hash": P.plan_hash(p["orders"], p["account_value_usd"], p.get("tranche"))}


def ready(p=None):
    """A plan recorded by `write_plan` and confirmed by the owner's token."""
    p = p or plan()
    P.write_plan(p)
    (E.CONFIRM / f"{p['plan_hash']}.token").write_text(p["plan_hash"])
    return p


def hand_made(orders, account=10000.0, held=None):
    """A plan whose orders were not produced by make_plan, hashed and recorded as the engine would (tranche 1, holding `held`)."""
    if held:
        LG.record_batch(LG.load(), 1, held)
    return ready({"orders": orders, "skipped": [], "account_value_usd": account, "plan_hash": P.plan_hash(orders, account, 1), "tranche": 1, "held": held or {},
                  "broker": "alpaca", "decision_month": THIS_MONTH, "slot": SLOT})


def events():
    return [r["event"] for r in A.records()]


class TestSendGates:
    def test_nothing_is_sent_without_the_owner_token_and_the_refusal_is_audited(self, broker):
        p = plan()
        P.write_plan(p)
        with pytest.raises(E.Refused, match="no owner confirmation"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events() == ["plan", "refused"]

    def test_a_token_written_for_another_plan_does_not_unlock_this_one(self, broker):
        p = plan()
        P.write_plan(p)
        (E.CONFIRM / f"{p['plan_hash']}.token").write_text("deadbeef")
        with pytest.raises(E.Refused, match="no owner confirmation"):
            E.send(p, True, sleep=0)
        assert broker.requests == []

    @pytest.mark.parametrize("breach,msg", [("preflight", "preflight"), ("kill", "kill switch"), ("tamper", "plan changed")])
    def test_a_confirmed_plan_is_refused_when_any_other_gate_fails(self, broker, breach, msg):
        p = ready()
        if breach == "kill":
            E.KILL.write_text("x")
        if breach == "tamper":
            p["orders"][0]["quantity"] += 1
        with pytest.raises(E.Refused, match=msg):
            E.send(p, breach != "preflight", sleep=0)
        assert broker.requests == []

    def test_a_confirmed_plan_that_was_never_recorded_by_the_engine_is_refused(self, broker):
        p = plan()
        (E.CONFIRM / f"{p['plan_hash']}.token").write_text(p["plan_hash"])
        with pytest.raises(E.Refused, match="no plan record"):
            E.send(p, True, sleep=0)
        assert broker.requests == []

    def test_a_plan_that_names_no_broker_is_refused(self, broker):
        p = plan()
        del p["broker"]
        with pytest.raises(E.Refused, match="plan is for broker ''"):
            E.send(ready(p), True, sleep=0)
        assert broker.requests == []

    def test_a_plan_relabelled_after_it_was_recorded_is_refused(self, broker):
        p = plan()
        del p["broker"]
        ready(p)                                                                    # recorded without a broker
        p["broker"] = "alpaca"
        with pytest.raises(E.Refused, match="differs from the one recorded"):
            E.check_gates(p, True)

    def test_a_plan_recorded_more_than_four_days_ago_must_be_made_again(self, broker):
        p = ready()
        made = dt.datetime.fromisoformat(A.records()[0]["utc"])
        E.check_gates(p, True, now=made + dt.timedelta(days=4))
        with pytest.raises(E.Refused, match="more than 4 days ago"):
            E.check_gates(p, True, now=made + dt.timedelta(days=4, seconds=1))

    def test_a_plan_is_sent_once_and_a_second_send_is_refused_even_with_its_token(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        with pytest.raises(E.Refused, match="already sent"):
            E.send(p, True, sleep=0)
        assert len(broker.requests) == 2 and events()[-1] == "refused"

    def test_an_altered_audit_log_stops_every_send(self, broker):
        p = ready()
        A.log("note", text="anything")
        ls = A.PATH.read_bytes().splitlines()
        A.PATH.write_bytes(b"\n".join([ls[0].replace(b'"orders": 2', b'"orders": 3'), ls[1]]) + b"\n")
        with pytest.raises(E.Refused, match=r"chain broken at line\(s\) \[2\]"):
            E.send(p, True, sleep=0)
        assert broker.requests == []

    @pytest.mark.parametrize("orders,msg", [
        ([{"ticker": "AAA", "quantity": 1.0, "usd": 100.0, "side": "SELL"}], r"malformed orders: \['AAA'\]"),           # side disagrees with the sign
        ([{"ticker": "AAA", "quantity": 0.0, "usd": 100.0, "side": "SELL"}], r"malformed orders: \['AAA'\]"),
        ([{"ticker": "AAA", "quantity": 1.0, "usd": 100.0, "side": "BUY"}] * 2, "a ticker appears twice"),
        ([{"ticker": "AAA", "quantity": 100.0, "usd": 10000.0, "side": "BUY"}], "above 10%"),
        ([{"ticker": "AAA_EQ", "quantity": 1.0, "usd": 100.0, "side": "BUY"}], r"not Alpaca US symbols: \['AAA_EQ'\]"),
        ([{"ticker": f"T{c}", "quantity": 1.0, "usd": 950.0, "side": "BUY"} for c in "ABCDEFGHIJK"], "buys total 10,450.00 USD, more than the account's 10,000.00"),
    ])
    def test_an_order_set_outside_the_size_and_form_rules_is_refused(self, broker, orders, msg):
        p = hand_made(orders)
        with pytest.raises(E.Refused, match=msg):
            E.send(p, True, sleep=0)
        assert broker.requests == []

    def test_sells_do_not_count_toward_the_buy_limit(self, broker):
        names = [f"T{c}" for c in "ABCDEFGHIJK"]
        E.check_gates(hand_made([{"ticker": t, "quantity": -1.0, "usd": 950.0, "side": "SELL"} for t in names], held={t: 1.0 for t in names}), True)

    def test_a_batch_whose_positions_cannot_be_read_first_is_refused_and_audited(self, broker):
        p = ready()
        broker.positions = OSError("network down")
        with pytest.raises(E.Refused, match="positions could not be read: OSError"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events() == ["plan", "refused"]


class TestRustGate:
    """Amendment A7: rust_core's pre-trade check runs last, in-process, and the plan is refused if it objects or cannot run."""

    @pytest.mark.parametrize("usd", [float("nan"), 0.0, -500.0])
    def test_an_order_value_that_is_not_a_positive_number_is_refused_by_python_and_independently_by_rust(self, broker, usd):
        p = hand_made([{"ticker": "AAA", "quantity": 100.0, "usd": usd, "side": "BUY"}])
        with pytest.raises(E.Refused, match=r"malformed orders: \['AAA'\]"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events()[-1] == "refused"
        assert any(r.startswith("malformed orders") for r in E.KN.pre_trade_check(json.dumps(p), False, True, p["plan_hash"]))      # rust_core says the same

    @pytest.mark.parametrize("mode", ["auto", "python"])
    def test_without_a_current_rust_library_every_plan_is_refused_whatever_the_kernel_setting(self, broker, tmp_path, monkeypatch, mode):
        from quant.core import kernels as KN
        monkeypatch.setenv("QUANT_RUST_CORE_DLL", str(tmp_path / "absent" / "rust_core.dll"))
        monkeypatch.setenv("QUANT_KERNELS", mode)
        monkeypatch.setattr(KN, "_STATE", {})
        monkeypatch.setattr(KN, "_LIB", None)
        with pytest.raises(E.Refused, match="Rust pre-trade check could not run: no library at"):
            E.send(ready(), True, sleep=0)
        assert broker.requests == []

    def test_a_valid_confirmed_plan_passes_the_rust_check_and_is_sent(self, broker):
        E.send(ready(), True, sleep=0)
        assert len(broker.requests) == 2 and "refused" not in events()


class TestOneSendAtATime:
    def test_a_send_while_the_lock_is_held_is_refused_and_leaves_the_lock_alone(self, broker):
        p = ready()
        E.LOCK.write_text("")
        with pytest.raises(E.Refused, match="another send is running"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and E.LOCK.exists() and events() == ["plan", "refused"]

    def test_the_lock_is_released_after_a_batch_and_after_a_refusal(self, broker):
        E.send(ready(), True, sleep=0)
        assert not E.LOCK.exists()
        with pytest.raises(E.Refused):
            E.send(plan(tranche_fraction=0.5), True, sleep=0)
        assert not E.LOCK.exists()


class TestSend:
    def test_a_confirmed_plan_posts_market_on_open_orders_to_the_paper_host_with_its_keys(self, broker):
        p = ready()
        out = E.send(p, True, sleep=0)
        bodies = [b for _, b in broker.requests]
        assert [(b["symbol"], b["qty"], b["side"], b["type"], b["time_in_force"]) for b in bodies] == [("AAA", "10", "buy", "market", "opg"), ("BBB", "4", "sell", "market", "opg")]
        assert [b["client_order_id"] for b in bodies] == [f"{p['plan_hash'][:24]}-0", f"{p['plan_hash'][:24]}-1"]
        for req, _ in broker.requests:
            assert req.full_url == f"{PAPER}/v2/orders" and req.get_method() == "POST" and req.get_header("Apca-api-key-id") == "kid-x1"
        assert [r["response"]["id"] for r in out] == ["o1", "o2"] and events() == ["plan", "send_started", "sent", "sent", "send_finished"]

    def test_the_batch_record_holds_the_token_and_the_positions_before_the_first_order(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        started, finished = A.records()[1], A.records()[-1]
        assert started["token"] == str(E.CONFIRM / f"{p['plan_hash']}.token") and started["held_before"] == {"BBB": 4.0} and started["broker"] == "alpaca"
        assert (finished["accepted"], finished["errors"], finished["not_attempted"]) == (2, 0, 0)

    def test_no_api_key_or_secret_reaches_the_audit_log(self, broker):
        E.send(ready(), True, sleep=0)
        assert not any(k in A.PATH.read_text() for k in ("kid-x1", "secret-x2"))

    def test_an_order_that_fails_is_audited_and_the_batch_continues_without_retrying(self, broker):
        p = ready()
        broker.fail = {"AAA"}
        out = E.send(p, True, sleep=0)
        assert "connection reset" in out[0]["error"] and out[1]["response"]["id"] == "o2"
        assert len(broker.requests) == 2 and events() == ["plan", "send_started", "error", "sent", "send_finished"]

    def test_the_kill_switch_stops_the_batch_between_orders(self, broker, monkeypatch):
        p = ready()
        real = urllib.request.urlopen

        def urlopen_then_kill(req, timeout):
            if req.get_method() == "POST":
                E.KILL.write_text("stop")
            return real(req, timeout)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen_then_kill)
        E.send(p, True, sleep=0)
        assert len(broker.requests) == 1 and events()[-3:] == ["sent", "halted_by_kill_switch", "send_finished"] and A.records()[-1]["not_attempted"] == 1


class TestReconcile:
    def test_positions_that_differ_from_before_plus_orders_are_reported(self):
        orders = plan()["orders"]
        assert E.reconcile(orders, {"BBB": 4.0}, {"AAA": 10.0}) == []
        diffs = E.reconcile(orders, {"BBB": 4.0}, {"AAA": 10.0, "BBB": 4.0})            # the BBB sale did not happen
        assert diffs == [{"ticker": "BBB", "expected": 0.0, "actual": 4.0, "diff": 4.0}] and events() == []     # it writes nothing: the settled record follows the ledger

    def test_a_fraction_of_a_share_is_a_difference(self):
        assert E.reconcile([{"ticker": "AAA", "quantity": 0.0009}], {}, {})[0]["diff"] == -0.0009 and E.reconcile([{"ticker": "AAA", "quantity": 0.00001}], {}, {}) == []

    def test_a_sent_plan_is_reconciled_from_its_record_against_the_orders_the_broker_accepted(self, broker):
        p = ready()
        broker.fail = {"AAA"}
        E.send(p, True, sleep=0)
        broker.positions = []                                                    # the BBB sale filled; the failed AAA buy is an error, not a difference
        assert E.reconcile_sent(p) == []
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]
        assert E.reconcile_sent(p) == [{"ticker": "AAA", "expected": 0.0, "actual": 10.0, "diff": 10.0}]

    def test_a_plan_that_was_never_sent_cannot_be_reconciled(self, broker):
        with pytest.raises(E.Refused, match="never sent"):
            E.reconcile_sent(ready())


class TestPreflightGo:
    @pytest.fixture()
    def result(self, tmp_path):
        """The preflight result file, saying GO for `run_date`."""
        def write(run_date=None):
            run_date = run_date or dt.date.today()
            f = tmp_path / "preflight.json"
            f.write_text(json.dumps({"run_date": str(run_date), "go": True}))
            return f
        return write

    def test_a_run_that_exits_zero_and_writes_todays_go_is_go(self, result):
        assert E.preflight_go([sys.executable, "-c", "pass"], result())

    def test_a_run_that_fails_is_not_go_even_if_an_earlier_run_today_wrote_go(self, result):
        assert not E.preflight_go([sys.executable, "-c", "raise SystemExit(1)"], result())

    def test_a_go_written_on_an_earlier_day_is_not_go(self, result):
        assert not E.preflight_go([sys.executable, "-c", "pass"], result(dt.date.today() - dt.timedelta(days=1)))


def test_verify_audit_exits_one_when_the_chain_is_broken(capsys):
    P.write_plan(plan())
    A.log("note")
    assert E.main(["verify-audit"]) == 0
    A.PATH.write_bytes(A.PATH.read_bytes().splitlines()[1] + b"\n")
    assert E.main(["verify-audit"]) == 1 and '"broken": [\n  1\n ]' in capsys.readouterr().out


class TestCommands:
    @pytest.fixture()
    def preflight(self, tmp_path, monkeypatch):
        """The preflight the send command runs: a command that exits with `code`, and today's GO result file."""
        def set_exit(code):
            result = tmp_path / "preflight.json"
            result.write_text(json.dumps({"run_date": str(dt.date.today()), "go": True}))
            monkeypatch.setattr(E, "PREFLIGHT", ([sys.executable, "-c", f"raise SystemExit({code})"], result))
        return set_exit

    def test_send_and_reconcile_run_from_the_plan_file(self, broker, preflight, capsys):
        p = ready()
        preflight(0)
        f = str(P.PLANS / f"{p['plan_hash']}.json")
        assert E.main(["send", f]) == 0 and [b["symbol"] for _, b in broker.requests] == ["AAA", "BBB"]
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]                 # both orders filled
        capsys.readouterr()
        assert E.main(["reconcile", f]) == 0 and json.loads(capsys.readouterr().out) == []

    def test_a_refused_send_prints_one_line_exits_one_and_sends_nothing(self, broker, preflight, capsys):
        p = ready()
        preflight(1)                                                                      # preflight said NO-GO
        assert E.main(["send", str(P.PLANS / f"{p['plan_hash']}.json")]) == 1
        assert capsys.readouterr().out.strip() == "REFUSED: preflight is not GO" and broker.requests == [] and events()[-1] == "refused"


class TestTrancheLedger:
    """A6: each tranche starts its plan from the holdings recorded for it, and only a batch reconciled with zero differences changes them."""

    def test_a_plan_that_names_no_tranche_is_refused(self, broker):
        p = plan()
        del p["tranche"]
        p = rehash(p)                                                              # a valid hash, so the tranche gate is what refuses
        with pytest.raises(E.Refused, match="names no tranche"):
            E.send(ready(p), True, sleep=0)
        assert broker.requests == []

    def test_a_clean_reconciliation_records_the_tranches_new_holdings(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]                 # both orders filled
        assert E.reconcile_sent(p) == [] and LG.tranche(LG.load(), 0) == {"AAA": 10.0} and events()[-2:] == ["ledger_updated", "reconciled"]   # the ledger first

    def test_a_reconciliation_with_differences_leaves_the_ledger_and_blocks_the_tranche(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        broker.positions = [{"symbol": "AAA", "qty": "7", "side": "long"}]                  # a partial fill
        assert E.reconcile_sent(p) != [] and LG.tranche(LG.load(), 0) == {"BBB": 4.0}
        assert E.unsettled(0) == p["plan_hash"] and E.unsettled(1) is None

    def test_another_plan_of_an_unsettled_tranche_is_refused_until_the_last_batch_reconciles(self, broker):
        first = ready()
        E.send(first, True, sleep=0)
        second = ready(plan(tranche_fraction=0.5))
        with pytest.raises(E.Refused, match="has not reconciled"):
            E.send(second, True, sleep=0)
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]
        E.reconcile_sent(first)                                                         # settled: the tranche now holds 10 AAA, not the 4 BBB the second plan assumed
        with pytest.raises(E.Refused, match="holdings changed since the plan"):
            E.send(second, True, sleep=0)
        assert len(broker.requests) == 2

    def test_the_plan_command_starts_from_the_ledger_and_the_books_overlay(self, tmp_path, yahoo, broker, capsys):
        book = tmp_path / "book.csv"
        pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [0.6, 0.4]}).to_csv(book, index=False)
        book.with_suffix(".json").write_text(json.dumps({"overlay_state": {"exposure_now": 0.5}, "decision_month": THIS_MONTH}))
        assert E.main(["plan", str(book), "--account-usd", "10000", "--slot", "2026-11-24"]) == 0     # ISO week 48: tranche 0, which holds 4 BBB
        p = json.loads(next(P.PLANS.glob("*.json")).read_text())
        assert (p["tranche"], p["held"], p["exposure"], p["exposure_source"], p["held_source"]) == (0, {"BBB": 4.0}, 0.5, "target book overlay state", "ledger, tranche 0")
        assert {o["ticker"]: o["quantity"] for o in p["orders"]} == {"AAA": 15.0, "BBB": 21.0}     # 1250 USD: 750 / 50 = 15 AAA; 500 / 20 = 25 BBB, 4 held
        assert p["broker"] == "alpaca" and p["decision_month"] == THIS_MONTH and broker.requests == []

    def test_the_plan_command_plans_another_tranche_only_on_an_amended_catch_up_day(self, tmp_path, yahoo, broker, capsys):
        book = tmp_path / "book.csv"
        pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [0.6, 0.4]}).to_csv(book, index=False)
        book.with_suffix(".json").write_text(json.dumps({"overlay_state": {"exposure_now": 1.0}, "decision_month": THIS_MONTH}))
        assert E.main(["plan", str(book), "--account-usd", "10000", "--slot", "2026-12-01", "--tranche", "3"]) == 1
        assert "REFUSED: tranche 3 is not planned on the slot of 2026-12-01" in capsys.readouterr().out
        assert E.main(["plan", str(book), "--account-usd", "10000", "--slot", "2026-11-24", "--tranche", "2"]) == 0     # A30
        p = json.loads(next(P.PLANS.glob("*.json")).read_text())
        assert (p["tranche"], p["held_source"]) == (2, "ledger, tranche 2")

    def test_the_plan_command_refuses_a_tranche_whose_last_batch_is_unsettled(self, tmp_path, broker, capsys):
        E.send(ready(), True, sleep=0)
        capsys.readouterr()
        assert E.main(["plan", str(tmp_path / "book.csv"), "--account-usd", "10000", "--slot", "2026-11-24"]) == 1
        assert capsys.readouterr().out.startswith("REFUSED: tranche 0's last batch")


class TestToken:
    @pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])                 # utf-16 with a byte-order mark is what `echo hash > file` writes in Windows PowerShell 5.1
    def test_the_owners_token_is_read_whatever_editor_wrote_it(self, broker, encoding):
        p = plan()
        P.write_plan(p)
        (E.CONFIRM / f"{p['plan_hash']}.token").write_bytes(f"{p['plan_hash']}\r\n".encode(encoding))
        E.send(p, True, sleep=0)
        assert len(broker.requests) == 2

    def test_a_token_that_is_not_text_is_refused_and_audited_not_a_crash(self, broker):
        p = plan()
        P.write_plan(p)
        (E.CONFIRM / f"{p['plan_hash']}.token").write_bytes(b"\x80\x81\x82")
        with pytest.raises(E.Refused, match="no owner confirmation"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events()[-1] == "refused"


class TestAlpaca:
    """Amendment A10 specifics: the asset list, the opening-auction orders (A13) and their fallback. HTTP replaced at urlopen."""

    @pytest.fixture()
    def alp(self, broker):
        return broker

    @staticmethod
    def alpaca_plan():
        return ready()

    def test_a_symbol_missing_from_the_asset_list_is_refused(self, alp, monkeypatch):
        monkeypatch.setattr(E, "alpaca_tradable", lambda: {"BBB"})
        with pytest.raises(E.Refused, match=r"not active and tradable.*\['AAA'\]"):
            E.check_gates(self.alpaca_plan(), True)

    def test_without_an_asset_list_nothing_is_sent(self, alp, monkeypatch):
        def none():
            raise FileNotFoundError("no Alpaca asset list")
        monkeypatch.setattr(E, "alpaca_tradable", none)
        with pytest.raises(E.Refused, match="no Alpaca asset list"):
            E.send(self.alpaca_plan(), True, sleep=0)
        assert alp.requests == []

    def test_reconciliation_reads_alpaca_positions(self, alp):
        p = self.alpaca_plan()
        E.send(p, True, sleep=0)
        alp.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]
        assert E.reconcile_sent(p) == [] and LG.tranche(LG.load(), 0) == {"AAA": 10.0}

    def test_inside_the_closed_window_nothing_is_sent_and_the_refusal_is_logged(self, alp, monkeypatch):
        monkeypatch.setattr(E, "CLOCK", lambda: dt.datetime(2026, 11, 17, 15, 0, tzinfo=dt.timezone.utc))
        with pytest.raises(E.Refused, match="rejects opening-auction orders"):
            E.send(self.alpaca_plan(), True, sleep=0)
        assert alp.requests == [] and A.records()[-1]["event"] == "refused"

    def test_an_opening_auction_order_alpaca_refuses_is_sent_once_as_a_day_order_and_logged(self, alp, monkeypatch):
        sent = []

        def post(body):
            sent.append(body)
            if body["time_in_force"] == "opg" and body["symbol"] == "AAA":
                raise urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b'{"message": "opg not enabled"}'))
            return {"id": f"o{len(sent)}"}
        monkeypatch.setattr(E, "alpaca_post", post)
        p = self.alpaca_plan()
        out = E.send(p, True, sleep=0)
        assert [(b["symbol"], b["time_in_force"]) for b in sent] == [("AAA", "opg"), ("AAA", "day"), ("BBB", "opg")]
        assert sent[1]["client_order_id"].endswith("-0d") and all("response" in x for x in out)
        assert [r["status"] for r in A.records() if r["event"] == "opg_refused_day_sent"] == [403]

    def test_a_server_error_is_not_retried_as_a_day_order(self, alp, monkeypatch):
        def post(body):
            raise urllib.error.HTTPError("u", 503, "unavailable", {}, io.BytesIO(b""))
        monkeypatch.setattr(E, "alpaca_post", post)
        out = E.send(self.alpaca_plan(), True, sleep=0)
        assert all("error" in x for x in out) and not [r for r in A.records() if r["event"] == "opg_refused_day_sent"]

    def test_the_rust_check_accepts_a_valid_alpaca_plan_on_its_own(self, alp):
        p = self.alpaca_plan()
        assert E.KN.pre_trade_check(json.dumps(p), False, True, p["plan_hash"]) == []


def test_a_line_goes_out_as_its_whole_shares_on_the_open_and_its_fraction_as_a_day_order():
    h = "f" * 64
    split = E.alpaca_orders({"ticker": "AAA", "quantity": 12.3456}, h, 3)
    assert [(b["qty"], b["time_in_force"], b["client_order_id"]) for b in split] == [("12", "opg", f"{h[:24]}-3"), ("0.3456", "day", f"{h[:24]}-3f")]
    assert [(b["qty"], b["time_in_force"], b["side"]) for b in E.alpaca_orders({"ticker": "AAA", "quantity": -7.0}, h, 0)] == [("7", "opg", "sell")]
    assert [(b["qty"], b["time_in_force"]) for b in E.alpaca_orders({"ticker": "AAA", "quantity": 0.25}, h, 0)] == [("0.25", "day")]
    assert [b["qty"] for b in E.alpaca_orders({"ticker": "AAA", "quantity": 3.99999999}, h, 0)] == ["4"]       # float noise is not a fraction


@pytest.mark.parametrize("utc,closed", [
    ("2026-11-17T14:27:00", False),   # Tuesday 09:27 New York: before the cut-off
    ("2026-11-17T14:28:00", True),    # 09:28: Alpaca rejects opening-auction orders from here
    ("2026-11-17T23:59:00", True),    # 18:59
    ("2026-11-18T00:00:00", False),   # 19:00: queued for the next open
    ("2026-11-21T16:00:00", False),   # Saturday
    ("2026-07-14T13:27:00", False),   # summer time: 09:27 New York is 13:27 UTC
    ("2026-07-14T13:28:00", True),
])
def test_the_send_window_follows_new_york_time_and_its_daylight_saving(utc, closed):
    assert (E.auction_window_problem(dt.datetime.fromisoformat(utc).replace(tzinfo=dt.timezone.utc)) is not None) is closed


def test_alpaca_quantities_keep_four_decimals_without_trailing_zeros():
    assert (E.qty_text(10.0), E.qty_text(-0.125), E.qty_text(1.23456)) == ("10", "0.125", "1.2346")


@pytest.mark.skip(reason="the example book is long only (quant.portfolio.model.Execution.shorts is False)")
class TestShortSales:
    """Amendment A11: the short-sale gates on the Alpaca path. HTTP replaced; asset lists replaced by fixed sets."""

    @pytest.fixture()
    def alp(self, monkeypatch):
        monkeypatch.setattr(E, "SEND_BROKER", "alpaca")
        monkeypatch.setattr(E, "alpaca_tradable", lambda: {"AAA", "ZZZ", "HTB"})
        monkeypatch.setattr(E, "alpaca_shortable", lambda: {"AAA", "ZZZ"})
        monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "kid-x1")
        monkeypatch.setenv("ALPACA_PAPER_SECRET", "secret-x2")
        state = types.SimpleNamespace(requests=[])

        def urlopen(req, timeout):
            if req.get_method() == "GET":
                return io.BytesIO(b'{"equity": "100000"}' if req.full_url.endswith("/v2/account") else b'[{"symbol": "BBB", "qty": "4", "side": "long", "market_value": "80"}]' if req.full_url.endswith("/v2/positions") else b"[]")
            state.requests.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"id": "o", "status": "accepted"}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return state

    @staticmethod
    def signed(orders, sf=0.3, held=None, account=100000.0, lev=None):
        return ready({"orders": orders, "skipped": [], "account_value_usd": account, "plan_hash": P.plan_hash(orders, account, 1), "tranche": 1,
                      "held": held or {}, "broker": "alpaca", "decision_month": THIS_MONTH, "slot": SLOT, **({"short_fraction": sf} if sf is not None else {}), **({"leverage": lev} if lev is not None else {})})

    def test_a_130_30_tranche_with_whole_share_shorts_is_sent_as_sells(self, alp):
        p = self.signed([{"ticker": "AAA", "quantity": 10.0, "usd": 1000.0, "side": "BUY"}, {"ticker": "ZZZ", "quantity": -5.0, "usd": 500.0, "side": "SELL"}])
        E.send(p, True, sleep=0)
        assert [(b["symbol"], b["qty"], b["side"]) for b in alp.requests] == [("AAA", "10", "buy"), ("ZZZ", "5", "sell")]

    def test_a_short_sale_without_a_declared_fraction_is_refused(self, alp):
        with pytest.raises(E.Refused, match=r"without a declared short fraction: \['ZZZ'\]"):
            E.check_gates(self.signed([{"ticker": "ZZZ", "quantity": -5.0, "usd": 500.0, "side": "SELL"}], sf=None), True)

    def test_a_fractional_short_sale_is_refused(self, alp):
        with pytest.raises(E.Refused, match="not whole shares"):
            E.check_gates(self.signed([{"ticker": "ZZZ", "quantity": -2.5, "usd": 250.0, "side": "SELL"}]), True)

    def test_a_hard_to_borrow_name_is_never_shorted(self, alp):
        with pytest.raises(E.Refused, match=r"not shortable and easy to borrow: \['HTB'\]"):
            E.check_gates(self.signed([{"ticker": "HTB", "quantity": -5.0, "usd": 500.0, "side": "SELL"}]), True)

    def test_selling_a_long_holding_to_zero_needs_no_short_permission(self, alp):
        LG.record_batch(LG.load(), 1, {"HTB": 5.0})
        p = self.signed([{"ticker": "HTB", "quantity": -5.0, "usd": 500.0, "side": "SELL"}], sf=None, held={"HTB": 5.0})
        E.check_gates(p, True)                                                     # every gate passes, Rust included: a plain sale is not a short sale

    def test_short_sales_above_the_tranche_cap_are_refused(self, alp):
        orders = [{"ticker": t, "quantity": -10.0, "usd": 5500.0, "side": "SELL"} for t in ("AAA", "ZZZ")]
        with pytest.raises(E.Refused, match="short sales total 11,000.00 USD, more than 10,500.00"):
            E.check_gates(self.signed(orders), True)

    def test_the_buy_limit_rises_with_the_short_fraction_only(self, alp, monkeypatch):
        orders = [{"ticker": t, "quantity": 1.0, "usd": 9900.0, "side": "BUY"} for t in "ABCDEFGHIJKL"]
        monkeypatch.setattr(E, "alpaca_tradable", lambda: {o["ticker"] for o in orders})
        with pytest.raises(E.Refused, match=r"more than \(1 \+ 0.1\) x the account"):
            E.check_gates(self.signed(orders, sf=0.1), True)

    def test_a_levered_plan_may_buy_up_to_k_times_one_plus_s_of_the_account(self, alp, monkeypatch):
        k = 2.0 / 1.4
        orders = [{"ticker": f"B{c}", "quantity": 1.0, "usd": 9900.0, "side": "BUY"} for c in "ABCDEFGHIJKLMNOPQR"]
        monkeypatch.setattr(E, "alpaca_tradable", lambda: {o["ticker"] for o in orders})
        E.check_gates(self.signed(orders[:17], sf=0.2, lev=k), True)               # 168,300 <= 171,428.57: every gate passes, Rust included
        with pytest.raises(E.Refused, match=r"more than 1.428\d* x \(1 \+ 0.2\) x the account"):
            E.check_gates(self.signed(orders, sf=0.2, lev=k), True)

    def test_leverage_beyond_the_gross_cap_is_refused(self, alp):
        with pytest.raises(E.Refused, match=r"leverage 1.3 outside"):
            E.check_gates(self.signed([{"ticker": "AAA", "quantity": 1.0, "usd": 100.0, "side": "BUY"}], sf=0.3, lev=1.3), True)

    def test_a_short_fraction_above_the_cap_is_refused(self, alp):
        with pytest.raises(E.Refused, match="outside"):
            E.check_gates(self.signed([{"ticker": "AAA", "quantity": 1.0, "usd": 100.0, "side": "BUY"}], sf=0.5), True)


class TestHardening:
    """Amendment A15: the plan file is sealed, the account value is checked against the equity, reconciliation needs a finished send and looks up
    orders whose send failed."""

    def test_a_plan_edited_after_it_was_recorded_is_refused_even_with_a_valid_hash_and_token(self, broker):
        p = ready()
        p["held_source"] = "edited"                                                # outside the hash: only the seal sees it
        with pytest.raises(E.Refused, match="plan file changed since it was recorded"):
            E.send(p, True, sleep=0)
        assert broker.requests == []

    def test_a_plan_recorded_without_a_seal_must_be_made_again(self, broker):
        p = plan()
        A.log("plan", plan_hash=p["plan_hash"], orders=2, broker="alpaca")         # as written before A15
        (E.CONFIRM / f"{p['plan_hash']}.token").write_text(p["plan_hash"])
        with pytest.raises(E.Refused, match="without a digest"):
            E.check_gates(p, True)

    def test_a_plan_sized_on_more_than_the_accounts_equity_is_refused(self, broker):
        broker.equity = "9000"                                                     # the plan says 10,000: more than 5% above
        with pytest.raises(E.Refused, match="account's equity is 9,000.00 USD"):
            E.send(ready(), True, sleep=0)
        assert broker.requests == [] and events()[-1] == "refused"

    def test_an_unreadable_account_stops_the_send(self, broker, monkeypatch):
        def no_account():
            raise OSError("network down")
        monkeypatch.setattr(E.ALP, "account", no_account)
        with pytest.raises(E.Refused, match="account equity could not be read"):
            E.send(ready(), True, sleep=0)

    def test_a_reconciliation_while_a_send_holds_the_lock_or_after_an_unfinished_send_is_refused(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        E.LOCK.write_text("")
        with pytest.raises(E.Refused, match="a send is running"):
            E.reconcile_sent(p)
        E.LOCK.unlink()
        q = ready(rehash(plan(tranche_fraction=0.5) | {"tranche": 1, "held": {}}))
        A.log("send_started", plan_hash=q["plan_hash"], held_before={}, broker="alpaca")
        with pytest.raises(E.Refused, match="no send_finished record"):
            E.reconcile_sent(q)

    def test_an_order_whose_send_failed_but_filled_counts_and_one_still_open_blocks(self, broker):
        p = ready()
        broker.fail = {"AAA"}
        E.send(p, True, sleep=0)
        cid = f"{p['plan_hash'][:24]}-0"
        broker.orders[cid] = {"status": "accepted", "filled_qty": "0"}
        with pytest.raises(E.Refused, match="still open at Alpaca"):
            E.reconcile_sent(p)
        broker.orders[cid] = {"status": "filled", "filled_qty": "10"}
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]
        assert E.reconcile_sent(p) == [] and LG.tranche(LG.load(), 0) == {"AAA": 10.0} and "errored_order_found_filled" in events()

    def test_the_broker_keys_never_follow_a_redirect(self, broker):
        E.send(ready(), True, sleep=0)
        req, _ = broker.requests[0]
        assert "Apca-api-key-id" not in req.headers and req.unredirected_hdrs["Apca-api-key-id"] == "kid-x1"


class TestRoundTwo:
    """Amendment A16: a stale book, a position that could not be priced, an account that no longer matches the ledger, orders still open at reconciliation."""

    def test_a_plan_from_an_old_book_or_without_a_decision_month_is_refused(self, broker):
        old = (dt.date.today().replace(day=1) - dt.timedelta(days=75)).strftime("%Y-%m")
        for k, (dm, msg) in enumerate(((old, "target book is for decision month"), (None, "names no valid decision month"), ("2026-13", "names no valid decision month"))):
            p = plan(tranche_fraction=0.9 - 0.1 * k) | {"decision_month": dm}              # a different fraction each time: each plan is recorded under its own hash
            with pytest.raises(E.Refused, match=msg):
                E.send(ready(p), True, sleep=0)
        assert broker.requests == []

    def test_a_plan_that_could_not_price_a_position_it_holds_is_refused(self, broker):
        p = plan() | {"skipped": [{"ticker": "BBB", "reason": "no_price"}]}                # BBB is held (4) and was not priced, so not sold
        with pytest.raises(E.Refused, match=r"could not price.*\['BBB'\]"):
            E.send(ready(p), True, sleep=0)
        assert broker.requests == []
        E.check_gates(ready(plan(tranche_fraction=0.5) | {"skipped": [{"ticker": "NEW", "reason": "no_price"}]}), True)   # a new name without a price is just not bought

    def test_an_account_that_no_longer_matches_the_ledger_stops_the_batch_before_the_first_order(self, broker):
        p = ready()
        broker.positions = [{"symbol": "BBB", "qty": "1", "side": "long"}]                # sold 3 BBB outside the engine: the plan's sale of 4 would open a short
        with pytest.raises(E.Refused, match=r"not what the tranche ledger holds.*BBB: account 1, ledger 4"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events()[-1] == "refused"

    def test_a_reconciliation_while_an_order_of_the_plan_is_still_open_is_refused(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        broker.open_orders = [{"client_order_id": f"{p['plan_hash'][:24]}-0f", "status": "new"}, {"client_order_id": "other-plan-0", "status": "new"}]
        broker.positions = []
        with pytest.raises(E.Refused, match=r"still open at Alpaca \(.*-0f\)"):
            E.reconcile_sent(p)
        assert "reconciled" not in events() and LG.tranche(LG.load(), 0) == {"BBB": 4.0}
        broker.open_orders = []
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]
        assert E.reconcile_sent(p) == []

    def test_a_tranches_dropped_holding_is_sold_by_the_plan_and_left_the_ledger_after_reconciling(self, broker):
        p = ready()                                                                       # BBB is held and not in the target: the plan sells it
        assert any(o["ticker"] == "BBB" and o["side"] == "SELL" for o in p["orders"])
        E.send(p, True, sleep=0)
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]
        assert E.reconcile_sent(p) == [] and "BBB" not in LG.tranche(LG.load(), 0)


class TestRoundThree:
    """Amendment A17: the slot date, the kill switch between the two orders of one line, the gross exposure of the whole account."""

    @pytest.mark.parametrize("slot, clock, message", [
        ("2026-11-17", dt.datetime(2026, 11, 20, 0, 5, tzinfo=dt.timezone.utc), "it is 2026-11-19 in New York"),       # Thursday evening for Tuesday's plan: would trade Friday's open
        ("2026-11-24", AFTER_CUTOFF, "slot of 2026-11-24"),                                                            # a future Tuesday's plan, sent a week early
        ("2026-11-18", AFTER_CUTOFF, "not a Tuesday"),
        (None, AFTER_CUTOFF, "no valid slot date"),
        ("next week", AFTER_CUTOFF, "no valid slot date")])
    def test_a_plan_is_refused_unless_sent_within_a_day_of_its_tuesday_slot(self, broker, monkeypatch, slot, clock, message):
        monkeypatch.setattr(E, "CLOCK", lambda: clock)
        with pytest.raises(E.Refused, match=message):
            E.check_gates(ready(plan() | {"slot": slot}), True)

    @pytest.mark.parametrize("clock", [dt.datetime(2026, 11, 17, 0, 5, tzinfo=dt.timezone.utc),             # the evening before the slot
                                       dt.datetime(2026, 11, 17, 22, 0, tzinfo=dt.timezone.utc),            # the slot's own evening
                                       dt.datetime(2026, 11, 18, 22, 0, tzinfo=dt.timezone.utc)])           # the day after
    def test_the_evening_before_the_slot_the_slot_and_the_day_after_are_all_accepted(self, broker, monkeypatch, clock):
        monkeypatch.setattr(E, "CLOCK", lambda: clock)
        E.check_gates(ready(), True)

    def test_the_kill_switch_stops_a_line_between_its_opening_auction_order_and_its_fractional_order(self, broker, monkeypatch):
        p = hand_made([{"ticker": "AAA", "quantity": 10.5, "usd": 525.0, "side": "BUY"}])         # 10 whole shares at the open, 0.5 share as a day order
        real = urllib.request.urlopen

        def urlopen_then_kill(req, timeout):
            if req.get_method() == "POST":
                E.KILL.write_text("stop")
            return real(req, timeout)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen_then_kill)
        E.send(p, True, sleep=0)
        assert [body["time_in_force"] for _req, body in broker.requests] == ["opg"] and "halted_by_kill_switch" in events()

    def test_a_line_whose_kill_switch_appeared_before_its_first_post_sends_nothing(self, broker):
        E.KILL.write_text("stop")
        with pytest.raises(E.Halted):
            E.send_alpaca_line({"ticker": "AAA", "quantity": 10.5, "usd": 525.0, "side": "BUY"}, "h" * 64, 0)
        assert broker.requests == []

    def test_a_batch_that_would_take_the_whole_account_above_the_gross_limit_is_refused(self, broker):
        limit = E.MAX_GROSS * 10000 * (1 + E.MAX_GROSS_EXCESS)
        broker.positions = [{"symbol": "BBB", "qty": "4", "side": "long", "market_value": "80"}, {"symbol": "ZZZ", "qty": "1", "side": "long", "market_value": str(limit - 600)}]
        p = ready()
        E.check_gates(p, True)                                                                        # 500 bought, 80 sold, ZZZ as held: just under the limit
        broker.positions[1]["market_value"] = str(limit - 400)
        with pytest.raises(E.Refused, match=r"gross exposure would be .* above"):
            E.check_gates(p, True)

    def test_a_short_position_counts_by_its_absolute_value(self):
        p = plan()
        assert E.gross_after(p, {"BBB": (4.0, 80.0), "ZZZ": (-5.0, -1000.0)}) == 500.0 + 1000.0

    def test_a_held_name_without_a_market_value_refuses_the_batch(self, broker):
        broker.positions = [{"symbol": "BBB", "qty": "4", "side": "long", "market_value": "80"}, {"symbol": "ZZZ", "qty": "1", "side": "long"}]
        with pytest.raises(E.Refused, match="ZZZ has no market value"):
            E.check_gates(ready(), True)


class TestRoundFour:
    """Amendment A18's neighbour in the same scan: short sales are judged on the account's holding, not the tranche's."""

    def test_a_sale_that_adds_to_another_tranches_short_in_the_same_name_is_a_short_sale(self, broker):
        LG.record_batch(LG.load(), 1, {"BBB": -10.0})                                  # tranche 1 is short 10 BBB; tranche 0 is long 4 BBB: the account is short 6
        p = ready()                                                                    # tranche 0 sells its 4 BBB: the account goes from -6 to -10
        assert [(o["ticker"], n) for o, n in E.short_sales(p)] == [("BBB", 4.0)]
        with pytest.raises(E.Refused, match=r"short sales without a declared short fraction: \['BBB'\]"):
            E.check_gates(p, True)

    def test_a_sale_that_only_reduces_a_long_account_position_is_not_a_short_sale(self, broker):
        assert E.short_sales(ready()) == []


class TestRoundFive:
    """Amendment A21: an order open at the broker blocks a send."""

    def test_an_order_open_at_the_broker_blocks_the_send_and_is_audited(self, broker):
        p = ready()
        broker.open_orders = [{"id": "x1", "client_order_id": "placed-by-hand", "status": "new"}]
        with pytest.raises(E.Refused, match=r"1 order\(s\) are open at the broker \(placed-by-hand\)"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events()[-1] == "refused"

    def test_with_no_open_order_the_batch_goes_out(self, broker):
        E.send(ready(), True, sleep=0)
        assert broker.requests


class TestCatchUpSiblings:
    """Amendment A30b: on a catch-up slot, the pending orders of the slot's other sent plans do not block a send and count in the gross check."""

    def test_a_catch_up_siblings_pending_orders_are_tolerated_but_any_other_open_order_still_blocks(self, broker, monkeypatch):
        p0 = ready()
        E.send(p0, True, sleep=0)
        p1 = hand_made([{"ticker": "AAA", "quantity": 2.0, "usd": 100.0, "side": "BUY"}])
        broker.open_orders = [{"client_order_id": f"{p0['plan_hash'][:24]}-0", "status": "new"}]
        with pytest.raises(E.Refused, match="are open at the broker"):
            E.send(p1, True, sleep=0)                                                   # not a catch-up slot: the A21 rule holds
        monkeypatch.setitem(SCH.CATCH_UP, dt.date.fromisoformat(SLOT), (0, 1))
        assert [s["plan_hash"] for s in E.catch_up_siblings(p1)] == [p0["plan_hash"]]
        broker.open_orders.append({"client_order_id": "placed-by-hand", "status": "new"})
        with pytest.raises(E.Refused, match=r"1 order\(s\) are open at the broker \(placed-by-hand\)"):
            E.send(p1, True, sleep=0)
        broker.open_orders.pop()
        n = len(broker.requests)
        E.send(p1, True, sleep=0)
        assert len(broker.requests) > n

    def test_the_gross_check_counts_a_siblings_pending_orders_as_filled(self):
        a = {"orders": [{"ticker": "AAA", "quantity": 10.0, "usd": 500.0}]}
        b = {"orders": [{"ticker": "AAA", "quantity": 5.0, "usd": 250.0}, {"ticker": "CCC", "quantity": -2.0, "usd": 40.0}]}
        m = {o["ticker"]: (o["quantity"], o["usd"]) for o in E.merged_orders([a, b])["orders"]}
        assert m == {"AAA": (15.0, 750.0), "CCC": (-2.0, 40.0)}


class TestDayOrders:
    """Amendment A31: one market day order per line, sendable while the market is open; tranches settled from their own orders' fills."""

    def test_a_line_is_one_market_day_order_for_its_whole_quantity(self, monkeypatch):
        monkeypatch.setattr(E, "DAY_ORDERS", True)
        assert E.alpaca_orders({"ticker": "AAA", "quantity": 8.5878}, "h" * 64, 3) == [
            {"symbol": "AAA", "qty": "8.5878", "side": "buy", "type": "market", "time_in_force": "day", "client_order_id": "h" * 24 + "-3"}]
        assert E.alpaca_orders({"ticker": "BBB", "quantity": -49.0}, "h" * 64, 0)[0]["qty"] == "49"

    def test_fills_settle_each_tranche_only_when_the_tranches_add_up_to_the_broker(self):
        LG.record_batch(LG.load(), 3, {"OLD": 10.0})
        base = {k: v for i in (1, 2) for k, v in LG.tranche(LG.load(), i).items()}
        assert not base                                                                  # only tranches 0 and 3 hold anything here
        a = {"plan_hash": "a" * 64, "tranche": 0, "held": LG.tranche(LG.load(), 0)}
        t0 = dict(a["held"])
        b = {"plan_hash": "b" * 64, "tranche": 3, "held": {"OLD": 10.0}}
        orders = [{"client_order_id": "a" * 24 + "-0", "symbol": "AAA", "side": "buy", "filled_qty": "3"},
                  {"client_order_id": "b" * 24 + "-0", "symbol": "OLD", "side": "sell", "filled_qty": "4"},
                  {"client_order_id": "b" * 24 + "-1", "symbol": "AAA", "side": "buy", "filled_qty": "0"},
                  {"client_order_id": "elsewhere-0", "symbol": "ZZZ", "side": "buy", "filled_qty": "9"}]
        new = E.settle_from_fills([a, b], orders, {**t0, "AAA": 3.0, "OLD": 6.0})
        assert new == {0: {**t0, "AAA": 3.0}, 3: {"OLD": 6.0}}
        with pytest.raises(E.Refused, match="do not add up"):
            E.settle_from_fills([a, b], orders, {**t0, "AAA": 3.0, "OLD": 7.0})
        with pytest.raises(E.Refused, match="ledger is not what plan"):
            E.settle_from_fills([{**b, "held": {}}], orders, {**t0, "OLD": 6.0})
