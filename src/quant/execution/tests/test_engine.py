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
from quant.execution import ledger as LG
from quant.portfolio import plan as P

PRICES = {"AAA": 50.0, "BBB": 20.0}
AFTER_CUTOFF = dt.datetime(2026, 11, 17, 0, 5, tzinfo=dt.timezone.utc)          # Monday 19:05 in New York: queued for Tuesday's opening auction
TRADABLE = {"AAA", "BBB", "BRK.B", *(f"T{c}" for c in "ABCDEFGHIJK")}
PAPER = "https://paper-api.alpaca.markets"


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


@pytest.fixture()
def broker(monkeypatch):
    """The Alpaca paper API behind urlopen: GET positions answers `positions` (or raises it), each POST is recorded and raises for the symbols in `fail`."""
    state = types.SimpleNamespace(requests=[], fail=set(), positions=[{"symbol": "BBB", "qty": "4", "side": "long"}])

    def urlopen(req, timeout):
        if req.get_method() == "GET":
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
    return {**P.make_plan({"AAA": 10.0, "BBB": 0.0}, {"BBB": 4.0}, PRICES, 10000.0, **kw), "tranche": 0, "held": {"BBB": 4.0}, "broker": "alpaca"}


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
    return ready({"orders": orders, "skipped": [], "account_value_usd": account, "plan_hash": P.plan_hash(orders, account), "tranche": 1, "held": held or {},
                  "broker": "alpaca"})


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

    def test_a_plan_recorded_more_than_four_days_ago_must_be_made_again(self):
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

    def test_sells_do_not_count_toward_the_buy_limit(self):
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

    def test_an_order_value_that_is_not_a_number_passes_the_python_gates_but_rust_refuses_it(self, broker):
        p = hand_made([{"ticker": "AAA", "quantity": 1.0, "usd": float("nan"), "side": "BUY"}])
        with pytest.raises(E.Refused, match=r"Rust pre-trade check: orders above 10% of the account: \['AAA'\]; malformed orders: \['AAA'\]"):
            E.send(p, True, sleep=0)
        assert broker.requests == [] and events()[-1] == "refused" and A.records()[-1]["reason"].startswith("Rust pre-trade check")

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
        assert E.reconcile("h", orders, {"BBB": 4.0}, {"AAA": 10.0}) == []
        diffs = E.reconcile("h", orders, {"BBB": 4.0}, {"AAA": 10.0, "BBB": 4.0})       # the BBB sale did not happen
        assert diffs == [{"ticker": "BBB", "expected": 0.0, "actual": 4.0, "diff": 4.0}] and events()[-1] == "reconciled"

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
        with pytest.raises(E.Refused, match="names no tranche"):
            E.send(ready(p), True, sleep=0)
        assert broker.requests == []

    def test_a_clean_reconciliation_records_the_tranches_new_holdings(self, broker):
        p = ready()
        E.send(p, True, sleep=0)
        broker.positions = [{"symbol": "AAA", "qty": "10", "side": "long"}]                 # both orders filled
        assert E.reconcile_sent(p) == [] and LG.tranche(LG.load(), 0) == {"AAA": 10.0} and events()[-1] == "ledger_updated"

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
        book.with_suffix(".json").write_text(json.dumps({"overlay_state": {"exposure_now": 0.5}}))
        assert E.main(["plan", str(book), "--account-usd", "10000", "--slot", "2026-11-24"]) == 0     # ISO week 48: tranche 0, which holds 4 BBB
        p = json.loads(next(P.PLANS.glob("*.json")).read_text())
        assert (p["tranche"], p["held"], p["exposure"], p["exposure_source"], p["held_source"]) == (0, {"BBB": 4.0}, 0.5, "target book overlay state", "ledger, tranche 0")
        assert {o["ticker"]: o["quantity"] for o in p["orders"]} == {"AAA": 15.0, "BBB": 21.0}     # 1250 USD: 750 / 50 = 15 AAA; 500 / 20 = 25 BBB, 4 held
        assert p["broker"] == "alpaca" and broker.requests == []

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
                return io.BytesIO(b"[]")
            state.requests.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"id": "o", "status": "accepted"}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return state

    @staticmethod
    def signed(orders, sf=0.3, held=None, account=100000.0, lev=None):
        return ready({"orders": orders, "skipped": [], "account_value_usd": account, "plan_hash": P.plan_hash(orders, account), "tranche": 1,
                      "held": held or {}, "broker": "alpaca", **({"short_fraction": sf} if sf is not None else {}), **({"leverage": lev} if lev is not None else {})})

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
