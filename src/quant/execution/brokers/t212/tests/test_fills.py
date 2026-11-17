"""quant.execution.brokers.t212.fills: what each accepted order realised against its plan. The engine's records are written by the engine itself (factories.Engine, through every
gate, against a broker replaced at urlopen); positions reads and order-history pages are parsed from bodies in the documented shapes, each with a journal
identity; the journal and the reports are real files in tmp_path. Times are taken from the batch the engine recorded."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest

from quant.execution import audit as A
from quant.execution import ledger as LG
from quant.portfolio import plan as P
from quant.execution.brokers.t212 import engine_records as ER
from quant.execution.brokers.t212 import fills as FL
from quant.execution.brokers.t212 import journal as J
from quant.execution.brokers.t212 import reports
from quant.execution.brokers.t212 import snapshots as SN
from quant.execution.brokers.t212.records import parse_historical_orders_page, parse_positions
from quant.execution.brokers.t212.tests import factories as F

PRICES = {"AAA_US_EQ": 50.0, "BBB_US_EQ": 20.0, "CCC_US_EQ": 10.0}
MIN = dt.timedelta(minutes=1)


def buy_two_aaa(engine, **answers):
    """Tranche 1 buys 2 AAA (100 USD at the plan's 50); the harness answers the order with id 501 unless `answers` says otherwise. Returns the batch."""
    engine.answers.update(answers)
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0}, PRICES))
    return ER.read_engine_state().batches[-1]


def opened(position, at):
    return {**position, "createdAt": at.isoformat()}


def snap(positions, at, sha="c" * 64):
    return SN.Snapshot(data=parse_positions(positions), record_sha256=sha, received_utc=at)


def page(items, at, sha="d" * 64):
    return SN.Snapshot(data=parse_historical_orders_page(F.page(items)).items, record_sha256=sha, received_utc=at)


def analyse(positions=(), history=()):
    state = ER.read_engine_state()
    return FL.analyse(state, {b.plan_hash: ER.load_plan(b.plan_hash) for b in state.batches}, positions, history, F.T0)


def filled_aaa(b, quantity=2.0, price=50.1, total_cost=80.0, minutes=10, sha="c" * 64):
    """A positions read `minutes` after batch `b` started, holding tranche 0's BBB and the AAA position the buy opened a minute after the start."""
    return snap([F.position("BBB_US_EQ", 4.0), opened(F.position("AAA_US_EQ", quantity, price, total_cost=total_cost), b.started_utc + MIN)],
                b.started_utc + minutes * MIN, sha)


def test_a_buy_that_opened_its_position_is_measured_from_the_first_positions_read_that_shows_it(engine):
    b = buy_two_aaa(engine)
    early = snap([F.position("BBB_US_EQ", 4.0)], b.started_utc + 5 * MIN, "1" * 64)
    later = filled_aaa(b, price=50.5, minutes=20, sha="3" * 64)
    (c,) = analyse([later, filled_aaa(b, sha="2" * 64), early]).costs
    e = c.execution
    assert (e.source, e.evidence, e.filled_shares, e.price_instrument_ccy, e.instrument_currency) == ("position_snapshot", ("2" * 64,), 2.0, 50.1, "USD")
    assert (e.filled_at_utc, e.observed_utc, e.wallet_currency, e.wallet_value_wallet_ccy) == (b.started_utc + MIN, b.started_utc + 10 * MIN, "GBP", 80.0)
    assert round(c.slippage_bp, 6) == 20.0 and round(c.slippage_usd, 6) == 0.2 and c.fill_ratio == 1.0
    assert e.fx_convention == "implied" and e.wallet_per_instrument == 80.0 / 100.2 and round(c.gross_value_wallet_ccy, 6) == 80.0
    assert e.fees_wallet_ccy is None and c.fees_bp is None and c.fx_fee_bp is None and c.all_in_cost_bp is None


def test_a_position_in_the_wallet_s_currency_has_an_fx_rate_of_one(engine):
    b = buy_two_aaa(engine)
    usd = F.position("AAA_US_EQ", 2.0, 50.0)
    usd["walletImpact"]["currency"] = "USD"
    (c,) = analyse([snap([opened(usd, b.started_utc + MIN)], b.started_utc + 10 * MIN)]).costs
    assert (c.execution.fx_convention, c.execution.wallet_per_instrument) == ("same_currency", 1.0)


def test_positions_read_before_the_order_or_after_the_next_batch_on_its_ticker_are_not_used(engine):
    first = buy_two_aaa(engine)
    engine.send(engine.plan(0, {"BBB_US_EQ": 4.0, "AAA_US_EQ": 1.0}, PRICES))             # tranche 0 buys 1 AAA in the next batch
    second = ER.read_engine_state().batches[-1]
    before = filled_aaa(first, minutes=-1, sha="1" * 64)
    after = snap([F.position("BBB_US_EQ", 4.0), opened(F.position("AAA_US_EQ", 2.0), second.started_utc + MIN)], second.started_utc + 10 * MIN, "2" * 64)
    r = analyse([before, after])
    assert [(u.plan_hash, u.reason) for u in r.unmeasured] == [
        (first.plan_hash, "no full positions read after the order and before the next batch trading the ticker"),
        (second.plan_hash, "the 1 positions read(s) after the order show 2 share(s), not the order's 1: not filled yet, partly filled, or changed")]


def test_a_buy_not_filled_yet_or_partly_filled_is_unmeasured_with_the_quantities_seen(engine):
    b = buy_two_aaa(engine)
    r = analyse([filled_aaa(b, quantity=1.5), snap([F.position("BBB_US_EQ", 4.0)], b.started_utc + 20 * MIN, "e" * 64)])
    assert [u.reason for u in r.unmeasured] == ["the 2 positions read(s) after the order show 1.5, 0 share(s), not the order's 2: not filled yet, partly filled, "
                                                "or changed"] and r.costs == ()


def test_with_no_positions_read_after_the_order_it_is_unmeasured(engine):
    buy_two_aaa(engine)
    assert [u.reason for u in analyse().unmeasured] == ["no full positions read after the order"]


def test_a_position_held_before_the_batch_is_not_measured_from_positions(engine):
    engine.send(engine.plan(1, {"BBB_US_EQ": 3.0}, PRICES))
    b = ER.read_engine_state().batches[-1]
    (u,) = analyse([snap([F.position("BBB_US_EQ", 7.0)], b.started_utc + 10 * MIN)]).unmeasured
    assert u.reason == "the position existed before the batch: its average price mixes earlier shares"


def test_a_position_opened_before_the_batch_is_not_taken_for_the_order_s_fill(engine):
    b = buy_two_aaa(engine)
    old = snap([opened(F.position("AAA_US_EQ", 2.0), b.started_utc - dt.timedelta(days=1))], b.started_utc + 10 * MIN)
    (u,) = analyse([old]).unmeasured
    assert u.reason == f"the position at the order's quantity was opened {(b.started_utc - dt.timedelta(days=1)).isoformat()}, before the batch started: not this order's fill"


def test_a_sale_needs_the_order_history(engine):
    engine.send(engine.plan(0, {}, PRICES))                                            # tranche 0 sells its 4 BBB
    (u,) = analyse().unmeasured
    assert (u.side, u.planned_shares, u.reason) == ("SELL", -4.0, "a sale does not show in positions: it needs the order history (INTEGRATION_SPEC.md)")


def test_an_answer_outside_the_documented_schema_is_still_measured_from_positions(engine):
    b = buy_two_aaa(engine, AAA_US_EQ={"id": 9})
    assert [c.execution.source for c in analyse([filled_aaa(b)]).costs] == ["position_snapshot"]


def test_history_fills_give_the_exact_price_fees_and_fx_rate_and_take_precedence_over_positions(engine):
    b = buy_two_aaa(engine)
    h = F.historical_order(501, "AAA_US_EQ", 2.0, price=50.2, fx_rate=0.8, net_value=2 * 50.2 * 0.8 + 0.12, filled_at="2026-10-26T14:30:01.250Z",
                           taxes=[F.tax("CURRENCY_CONVERSION_FEE", 0.12), F.tax("FINRA_FEE", 0.01, "USD")])
    (c,) = analyse([filled_aaa(b)], [page([h], b.started_utc + 30 * MIN, "e" * 64)]).costs
    e = c.execution
    assert (e.source, e.evidence, e.fill_ids, e.price_instrument_ccy, e.filled_shares, e.fx_rates_reported) == ("history_fill", ("e" * 64,), (5010,), 50.2, 2.0, (0.8,))
    assert (e.fx_convention, e.wallet_per_instrument, e.fees_wallet_ccy, e.fees_other_ccy) == ("wallet_per_instrument", 0.8, {"CURRENCY_CONVERSION_FEE": 0.12},
                                                                                              ("FINRA_FEE 0.01 USD",))
    assert e.filled_at_utc == dt.datetime(2026, 10, 26, 14, 30, 1, 250000, tzinfo=dt.UTC) and e.observed_utc == b.started_utc + 30 * MIN
    assert round(c.slippage_bp, 6) == 40.0 and round(c.gross_value_wallet_ccy, 6) == 80.32 and round(c.fees_bp, 6) == round(0.12 / 80.32 * 1e4, 6)
    assert c.fx_fee_bp == c.fees_bp and round(c.all_in_cost_bp, 6) == round(40.0 + 0.12 / 80.32 * 1e4, 6) and round(e.wallet_value_wallet_ccy, 6) == 80.44


def test_the_plan_price_is_the_previous_close_when_its_date_is_the_weekday_before_the_fill(engine):
    buy_two_aaa(engine)                                                                  # the plan's price date is Friday 23 October 2026
    monday, tuesday = (F.historical_order(501, filled_at=f"2026-10-{d}T14:30:01Z") for d in (26, 27))
    assert analyse(history=[page([monday], F.T0)]).costs[0].plan_price_is_previous_close is True
    assert analyse(history=[page([tuesday], F.T0)]).costs[0].plan_price_is_previous_close is False


def test_without_a_price_date_the_previous_close_is_not_judged(engine):
    buy_two_aaa(engine)
    state = ER.read_engine_state()
    plans = {b.plan_hash: dataclasses.replace(ER.load_plan(b.plan_hash), price_date=None) for b in state.batches}
    (c,) = FL.analyse(state, plans, [], [page([F.historical_order(501)], F.T0)], F.T0).costs
    assert c.plan_price_is_previous_close is None and c.to_json()["plan_price_date"] is None


@pytest.mark.parametrize("fx_rate,net,wallet,convention,per_unit", [
    (1.25, 100.4 / 1.25, "GBP", "instrument_per_wallet", 0.8),
    (0.8, 50.0, "GBP", "undetermined", None),                                           # neither reading reproduces the net value
    (1.0, 100.4, "GBP", "undetermined", None),                                          # both do: the direction cannot be told
    (0.0, 100.4, "GBP", "undetermined", None),
    (1.0, 100.4, "USD", "same_currency", 1.0),
])
def test_the_fx_convention_is_the_reading_that_reproduces_the_net_value(engine, fx_rate, net, wallet, convention, per_unit):
    buy_two_aaa(engine)
    h = F.historical_order(501, "AAA_US_EQ", 2.0, price=50.2, fx_rate=fx_rate, net_value=net)
    h["fill"]["walletImpact"]["currency"] = wallet
    (c,) = analyse(history=[page([h], F.T0)]).costs
    assert c.execution.fx_convention == convention and (c.execution.wallet_per_instrument == per_unit if per_unit is None else
                                                        round(c.execution.wallet_per_instrument, 9) == per_unit)
    assert (c.fees_bp is None) == (per_unit is None)


def test_several_fills_of_one_order_are_averaged_by_quantity_and_a_repeated_fill_counts_once(engine):
    buy_two_aaa(engine)
    h1 = F.historical_order(501, "AAA_US_EQ", 1.5, price=50.0)
    h2 = F.historical_order(501, "AAA_US_EQ", 0.5, price=50.4, filled_at="2026-10-27T14:30:05Z")
    h2["fill"]["id"] = 5011
    (c,) = analyse(history=[page([h1, h2], F.T0, "e" * 64), page([h1], F.T0 + MIN, "f" * 64)]).costs
    e = c.execution
    assert (e.fill_ids, e.filled_shares, e.evidence, e.filled_at_utc) == ((5010, 5011), 2.0, ("e" * 64,), dt.datetime(2026, 10, 27, 14, 30, 5, tzinfo=dt.UTC))
    assert round(e.price_instrument_ccy, 9) == 50.1 and e.fees_wallet_ccy == {"CURRENCY_CONVERSION_FEE": 1.2} and round(e.wallet_per_instrument, 12) == 0.8


def test_a_fee_is_taken_as_an_absolute_amount(engine):
    buy_two_aaa(engine)
    (c,) = analyse(history=[page([F.historical_order(501, taxes=[F.tax(amount=-0.6)])], F.T0)]).costs
    assert c.execution.fees_wallet_ccy == {"CURRENCY_CONVERSION_FEE": 0.6}


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED"])
def test_an_order_the_history_says_did_not_execute_is_unmeasured_even_beside_a_position(engine, status):
    b = buy_two_aaa(engine)
    r = analyse([filled_aaa(b)], [page([{"order": F.order(501, status=status), "fill": None}], F.T0)])
    assert [u.reason for u in r.unmeasured] == [f"the order history says {status}: not executed"] and r.costs == ()


def test_an_order_without_a_trade_fill_in_the_history_falls_back_to_positions(engine):
    b = buy_two_aaa(engine)
    split = F.historical_order(501)
    split["fill"]["type"] = "STOCK_SPLIT"
    for item in ({"order": F.order(501, status="NEW"), "fill": None}, split):
        assert [c.execution.source for c in analyse([filled_aaa(b)], [page([item], F.T0)]).costs] == ["position_snapshot"]


def test_a_history_order_that_contradicts_the_plan_is_unmeasured(engine):
    buy_two_aaa(engine)
    (u,) = analyse(history=[page([F.historical_order(501, "BBB_US_EQ")], F.T0)]).unmeasured
    assert u.reason == "the order history's order 501 is a BUY of BBB_US_EQ; the plan's is a BUY of AAA_US_EQ"


def test_fills_in_more_than_one_wallet_currency_are_unmeasured(engine):
    buy_two_aaa(engine)
    h1, h2 = F.historical_order(501, quantity=1.0), F.historical_order(501, quantity=1.0)
    h2["fill"].update(id=5011, walletImpact={**h2["fill"]["walletImpact"], "currency": "EUR"})
    (u,) = analyse(history=[page([h1, h2], F.T0)]).unmeasured
    assert u.reason == "its fills are in more than one wallet currency: ['EUR', 'GBP']"


def test_a_history_sale_is_measured_with_its_shares_negative_and_slippage_adverse_when_received_less(engine):
    engine.send(engine.plan(0, {}, PRICES))                                            # tranche 0 sells 4 BBB at the plan's 20; the harness answers id 501
    (c,) = analyse(history=[page([F.historical_order(501, "BBB_US_EQ", -4.0, price=19.99)], F.T0)]).costs
    assert c.execution.filled_shares == -4.0 and round(c.slippage_bp, 6) == 5.0 and round(c.slippage_usd, 6) == 0.04 and c.fill_ratio == 1.0


def test_a_fill_in_another_currency_than_the_plan_s_has_no_slippage(engine):
    b = buy_two_aaa(engine)
    gbx = snap([F.position("BBB_US_EQ", 4.0), opened(F.position("AAA_US_EQ", 2.0, 4000.0, currency="GBX"), b.started_utc + MIN)], b.started_utc + 10 * MIN)
    r = analyse([gbx])
    assert r.costs[0].slippage_bp is None and r.costs[0].slippage_usd is None and r.slippage()["pass_rule_2"] == "not measurable"


@pytest.mark.parametrize("ccc_price,verdict,mean", [(10.01, "within", 5.33), (10.02, "outside", 8.67)])
def test_the_slippage_summary_gives_the_median_and_mean_of_absolute_slippage_and_pass_rule_2(engine, ccc_price, verdict, mean):
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0, "BBB_US_EQ": 3.0, "CCC_US_EQ": 5.0}, PRICES))              # answered with ids 501, 502, 503
    fills = [F.historical_order(501, "AAA_US_EQ", 2.0, price=50.01), F.historical_order(502, "BBB_US_EQ", 3.0, price=19.992),
             F.historical_order(503, "CCC_US_EQ", 5.0, price=ccc_price)]
    r = analyse(history=[page(fills, F.T0)])
    s = r.slippage()
    weighted = round((2.0 * 100.0 - 4.0 * 60.0 + (ccc_price / 10.0 - 1.0) * 1e4 * 50.0) / 210.0, 2)
    assert (s["orders"], s["median_abs_bp"], s["mean_abs_bp"], s["pass_rule_2"], s["weighted_mean_bp"]) == (3, 4.0, mean, verdict, weighted)
    assert s["worst_bp"] == round((ccc_price / 10.0 - 1.0) * 1e4, 2) and r.fees() == {"GBP": {"CURRENCY_CONVERSION_FEE": 1.8}}
    assert r.fx_conventions() == ["wallet_per_instrument"]


def test_with_nothing_measured_the_summary_says_so(engine):
    buy_two_aaa(engine)
    r = analyse()
    assert r.slippage() == {"orders": 0, "median_abs_bp": None, "mean_abs_bp": None, "weighted_mean_bp": None, "worst_bp": None, "pass_rule_2": "not measurable"}
    assert r.fees() == {} and r.fx_conventions() == [] and "Slippage against the plan's price (positive is adverse): none measured." in r.to_markdown()


def test_the_ledger_view_sets_a_fully_measured_unsettled_tranche_in_the_paper_ledger_s_own_format(engine, tmp_path):
    b = buy_two_aaa(engine)
    r = analyse([filled_aaa(b)])
    recorded = LG.record_batch(LG.load(), 1, {"AAA_US_EQ": 2.0}, path=tmp_path / "as_the_engine_writes.json")
    assert r.ledger_view == recorded == json.loads((tmp_path / "as_the_engine_writes.json").read_text()) and r.ledger_differences == ()
    assert r.ledger_batches == ((b.plan_hash, "tranche 1 set to the holdings its plan started from plus the shares its fills realised"),)
    assert r.to_json()["ledger_view"] == {"0": {"BBB_US_EQ": 4.0}, "1": {"AAA_US_EQ": 2.0}}


def test_a_partial_fill_shows_in_the_ledger_view_and_as_a_difference_from_the_engine_s_expectation(engine):
    buy_two_aaa(engine)
    r = analyse(history=[page([F.historical_order(501, "AAA_US_EQ", 1.5)], F.T0)])
    assert r.ledger_view["1"] == {"AAA_US_EQ": 1.5} and r.costs[0].fill_ratio == 0.75
    assert [d.to_json() for d in r.ledger_differences] == [{"tranche": 1, "ticker": "AAA_US_EQ", "expected_shares": 2.0, "realised_shares": 1.5,
                                                            "difference_shares": -0.5}]


def test_a_batch_with_an_unmeasured_order_leaves_its_tranche_as_recorded(engine):
    b = buy_two_aaa(engine)
    r = analyse()
    assert r.ledger_view == {"0": {"BBB_US_EQ": 4.0}} and r.ledger_batches == ((b.plan_hash, "1 accepted order(s) unmeasured: tranche 1 left as recorded"),)


def test_a_settled_batch_is_in_the_ledger_already_and_is_not_set_again(engine):
    b = buy_two_aaa(engine)
    engine.positions = [F.position("BBB_US_EQ", 4.0), F.position("AAA_US_EQ", 2.0)]
    assert engine.reconcile(json.loads((P.PLANS / f"{b.plan_hash}.json").read_text())) == []
    r = analyse([filled_aaa(b)])
    assert r.ledger_view == {"0": {"BBB_US_EQ": 4.0}, "1": {"AAA_US_EQ": 2.0}} and r.ledger_batches == () and len(r.costs) == 1
    assert "- no unsettled batch: the view is the recorded ledger" in r.to_markdown()


def test_a_batch_that_names_no_tranche_is_not_placed_in_the_ledger_view(engine):
    b = buy_two_aaa(engine)
    state = ER.read_engine_state()
    untranched = dataclasses.replace(state, batches=(dataclasses.replace(state.batches[0], tranche=None),))
    r = FL.analyse(untranched, {b.plan_hash: ER.load_plan(b.plan_hash)}, [filled_aaa(b)], [], F.T0)
    assert r.ledger_batches == ((b.plan_hash, "names no tranche: not placed in the ledger"),) and r.costs[0].tranche is None


def test_failed_and_unattempted_orders_are_counted_as_not_executed(engine):
    engine.answers["CCC_US_EQ"] = OSError("connection reset")
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0, "CCC_US_EQ": 5.0}, PRICES))
    assert analyse().counts() == {"orders_accepted": 1, "measured_from_history": 0, "measured_from_positions": 0, "unmeasured": 1, "orders_not_executed": 1}


def test_inputs_that_cannot_be_analysed_are_refused(engine):
    b = buy_two_aaa(engine)
    state = ER.read_engine_state()
    plans = {b.plan_hash: ER.load_plan(b.plan_hash)}
    with pytest.raises(FL.FillsInputError, match=f"plan\\(s\\) \\['{b.plan_hash[:12]}'\\] of the audit log's batches were not given"):
        FL.analyse(state, {}, [], [], F.T0)
    twice = snap([F.position("AAA_US_EQ", 1.0), F.position("AAA_US_EQ", 1.0)], b.started_utc + MIN, "9" * 64)
    with pytest.raises(FL.FillsInputError, match="positions read 999999999999 lists AAA_US_EQ twice"):
        FL.analyse(state, plans, [twice], [], F.T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        FL.analyse(state, plans, [], [], F.T0.replace(tzinfo=None))


def test_the_same_inputs_give_the_same_report_bytes_and_numbers_are_rounded(engine):
    b = buy_two_aaa(engine)
    reads = [filled_aaa(b, price=50.123456789, total_cost=80.123456)]
    one, two = analyse(reads), analyse(reads)
    assert reports.dumps(one.to_json()) == reports.dumps(two.to_json()) and one.to_markdown() == two.to_markdown()
    o = one.to_json()["orders"][0]
    assert o["execution"]["price_instrument_ccy"] == 50.123457 and o["execution"]["wallet_value_wallet_ccy"] == 80.12 and o["slippage_bp"] == 24.69
    assert o["sent_utc"].endswith("+00:00") and o["execution"]["filled_at_utc"].endswith("+00:00") and one.to_json()["sources"]["audit_head"] == A.verify()["head"]


def test_the_markdown_summary_states_what_was_measured_and_lists_orders_unmeasured_and_the_ledger_view(engine):
    engine.send(engine.plan(1, {"AAA_US_EQ": 2.0, "CCC_US_EQ": 5.0}, PRICES))
    md = analyse(history=[page([F.historical_order(501, "AAA_US_EQ", 2.0, price=50.1)], F.T0)]).to_markdown()
    assert md.startswith("# Trading 212 fills and costs\n") and "**1 of 2 accepted order(s) measured** (1 from the order history, 0 from positions); 1 unmeasured" in md
    assert "| AAA_US_EQ | BUY | 2 | 2 | 50.0000 | 50.1000 USD | +20.0 | +74.9 | history_fill |" in md          # fee 0.6 GBP of 2 x 50.1 x 0.8
    assert "| CCC_US_EQ | BUY | 5 | no full positions read after the order |" in md and "Fees: GBP CURRENCY_CONVERSION_FEE 0.6." in md
    assert "1 accepted order(s) unmeasured: tranche 1 left as recorded" in md and "FX convention of the history fills: wallet_per_instrument." in md


def test_long_tables_are_cut_to_the_newest_rows_in_the_markdown_and_kept_whole_in_the_json(engine):
    tickers = [f"Z{i:02d}_US_EQ" for i in range(FL.MAX_TABLE_ROWS + 1)]                    # one batch of 51 orders of 10 USD
    engine.send(engine.plan(1, {t: 1.0 for t in tickers}, {t: 10.0 for t in tickers}))
    r = analyse()
    md = r.to_markdown()
    assert "| Z00_US_EQ |" not in md and f"| {tickers[-1]} |" in md and f"The tables show the newest {FL.MAX_TABLE_ROWS} rows; the JSON report has them all." in md
    assert [u["ticker"] for u in r.to_json()["unmeasured"]] == tickers


def test_the_report_records_the_parameters_it_was_made_with(engine):
    buy_two_aaa(engine)
    assert analyse().to_json()["parameters"] == {"tolerance_shares": 0.001, "fx_match": 0.02, "pass_rule_2": {"median_abs_bp": 5.0, "mean_abs_bp": 8.0}}


def journal_positions(path, b, value=None, minutes=10):
    J.append(endpoint="positions", path="/equity/positions", pull=f"positions@{minutes}", page=0, status=200,
             body=F.body(value if value is not None else [F.position("BBB_US_EQ", 4.0), opened(F.position("AAA_US_EQ", 2.0, 50.1), b.started_utc + MIN)]),
             rate_limit=None, received_utc=b.started_utc + minutes * MIN, journal=path)


def test_the_command_analyses_the_journal_and_writes_both_reports(engine, tmp_path, capsys):
    b = buy_two_aaa(engine)
    journal_positions(tmp_path / "j.jsonl", b)
    assert FL.main(["--journal", str(tmp_path / "j.jsonl")], now=F.T0, out_dir=tmp_path / "out") == 0
    stem = tmp_path / "out" / f"fills_{reports.stamp(b.started_utc + 10 * MIN)}"
    payload = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["counts"]["measured_from_positions"] == 1 and payload["sources"]["positions_reads"] == [J.entries(tmp_path / "j.jsonl")[0].record_sha256]
    out = capsys.readouterr().out
    assert stem.with_suffix(".md").read_text(encoding="utf-8") in out and out.endswith(f"{stem.with_suffix('.json')}\n")


def test_the_command_leaves_the_engine_s_records_byte_for_byte_as_it_found_them(engine, tmp_path):
    b = buy_two_aaa(engine)
    journal_positions(tmp_path / "j.jsonl", b)
    records = [A.PATH, LG.LEDGER, *sorted(P.PLANS.iterdir())]
    before = {p: p.read_bytes() for p in records}
    assert FL.main(["--journal", str(tmp_path / "j.jsonl")], now=F.T0, out_dir=tmp_path / "out") == 0
    assert {p: p.read_bytes() for p in records} == before and sorted(P.PLANS.iterdir()) == records[2:]


def test_the_command_reads_journaled_order_history_pages(engine, tmp_path):
    b = buy_two_aaa(engine)
    J.append(endpoint=SN.HISTORY_ENDPOINT, path="/history", pull="h", page=0, status=200, body=F.body(F.page([F.historical_order(501)])), rate_limit=None,
             received_utc=b.started_utc + 10 * MIN, journal=tmp_path / "j.jsonl")
    assert FL.main(["--journal", str(tmp_path / "j.jsonl")], now=F.T0, out_dir=tmp_path / "out") == 0
    payload = json.loads((tmp_path / "out" / f"fills_{reports.stamp(b.started_utc + 10 * MIN)}.json").read_text(encoding="utf-8"))
    assert payload["counts"]["measured_from_history"] == 1 and payload["orders"][0]["execution"]["evidence"] == [J.entries(tmp_path / "j.jsonl")[0].record_sha256]


def _no_batch(tmp_path, engine):
    return


def _no_reads(tmp_path, engine):
    buy_two_aaa(engine)


def _broken_journal(tmp_path, engine):
    b = buy_two_aaa(engine)
    journal_positions(tmp_path / "j.jsonl", b)
    journal_positions(tmp_path / "j.jsonl", b, minutes=20)
    first, second = (tmp_path / "j.jsonl").read_bytes().splitlines()
    (tmp_path / "j.jsonl").write_bytes(first.replace(b'"page":0', b'"page":1') + b"\n" + second + b"\n")


def _bad_body(tmp_path, engine):
    journal_positions(tmp_path / "j.jsonl", buy_two_aaa(engine), value=[{**F.position(), "quantity": "2"}])


def _broken_audit(tmp_path, engine):
    buy_two_aaa(engine)
    first, *rest = A.PATH.read_bytes().splitlines()
    A.PATH.write_bytes(b"\n".join([first.replace(b"plan", b"plam", 1), *rest]) + b"\n")


def _plan_missing(tmp_path, engine):
    b = buy_two_aaa(engine)
    (P.PLANS / f"{b.plan_hash}.json").unlink()


def _report_exists(tmp_path, engine):
    b = buy_two_aaa(engine)
    journal_positions(tmp_path / "j.jsonl", b)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / f"fills_{reports.stamp(b.started_utc + 10 * MIN)}.json").write_text("kept")


@pytest.mark.parametrize("setup,message", [
    (_no_batch, "the audit log records no batch: nothing to analyse"),
    (_no_reads, "no positions or order-history read in"),
    (_broken_journal, "fails verification: broken line(s) [2]"),
    (_bad_body, "quantity: expected number"),
    (_broken_audit, "audit log chain broken"),
    (_plan_missing, ": no file "),
    (_report_exists, "reports are never overwritten"),
])
def test_the_command_refuses_with_exit_2_when_its_inputs_cannot_be_analysed(engine, tmp_path, capsys, setup, message):
    setup(tmp_path, engine)
    capsys.readouterr()
    assert FL.main(["--journal", str(tmp_path / "j.jsonl")], now=F.T0, out_dir=tmp_path / "out") == 2
    out = capsys.readouterr().out
    assert out.startswith("REFUSED: ") and message in out
    assert not list((tmp_path / "out").glob("*.md"))                                      # nothing written
