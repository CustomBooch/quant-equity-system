"""quant.execution.brokers.t212.records: every documented read response parsed into typed records, and each departure from the documented schema refused by name. Pure functions."""
from __future__ import annotations

import datetime as dt

import pytest

from quant.execution.brokers.t212 import records as R
from quant.execution.brokers.t212 import schema as S
from quant.execution.brokers.t212.tests import factories as F


def test_an_account_summary_is_read_with_the_account_currency_named_on_every_amount():
    s = R.parse_account_summary(F.account_summary(total=10000.0, available=2500.0, reserved=12.5))
    assert (s.account_id, s.account_currency, s.total_value_account_ccy) == (1001, "GBP", 10000.0)
    assert (s.cash.available_to_trade_account_ccy, s.cash.reserved_for_orders_account_ccy, s.cash.in_pies_account_ccy) == (2500.0, 12.5, 0.0)
    assert s.investments.current_value_account_ccy == 7500.0 and s.investments.total_cost_account_ccy == 7400.0


def test_positions_are_read_with_prices_in_the_instrument_currency_and_aware_timestamps():
    (p,) = R.parse_positions([F.position("AAA_US_EQ", quantity=2.5, average_price=40.0)])
    assert p.ticker == "AAA_US_EQ" and p.instrument.currency == "USD" and (p.quantity_shares, p.average_price_paid_instrument_ccy) == (2.5, 40.0)
    assert p.created_at_utc == dt.datetime(2026, 10, 20, 14, 31, 2, 120000, tzinfo=dt.UTC) and p.wallet_impact.fx_impact_wallet_ccy == 1.25


def test_a_position_in_the_account_currency_may_have_no_fx_impact():
    raw = F.position(currency="GBP")
    del raw["walletImpact"]["fxImpact"]
    assert R.parse_positions([raw])[0].wallet_impact.fx_impact_wallet_ccy is None


@pytest.mark.parametrize("drop", ["quantity", "averagePricePaid", "createdAt", "walletImpact", "instrument"])
def test_a_position_without_a_required_field_is_refused_at_its_path(drop):
    raw = F.position()
    del raw[drop]
    with pytest.raises(S.MissingField, match=rf"\$\[0\]\.{drop}: required field is absent"):
        R.parse_positions([raw])


def test_the_old_position_shape_with_a_top_level_ticker_is_refused_as_undocumented():
    raw = {**F.position(), "ticker": "AAA_US_EQ"}
    with pytest.raises(S.UnexpectedField, match=r"\$\[0\]: undocumented field\(s\) \['ticker'\]"):
        R.parse_positions([raw])


def test_a_quantity_sent_as_text_is_refused_not_coerced():
    with pytest.raises(S.WrongType, match=r"\$\[0\]\.quantity: expected number, got string"):
        R.parse_positions([{**F.position(), "quantity": "10"}])


class TestOrders:
    def test_a_market_order_is_read_with_its_status_and_optional_prices_absent(self):
        o = R.parse_order(F.order(order_id=77, quantity=-3.0, status="CONFIRMED"))
        assert (o.order_id, o.side, o.status, o.order_type, o.strategy, o.quantity_shares) == (77, "SELL", "CONFIRMED", "MARKET", "QUANTITY", -3.0)
        assert o.limit_price_order_ccy is None and o.stop_price_order_ccy is None and o.time_in_force is None

    def test_an_undocumented_status_is_refused(self):
        with pytest.raises(S.BadValue, match=r"\$\.status: 'DONE' is not a documented value"):
            R.parse_order({**F.order(), "status": "DONE"})

    @pytest.mark.parametrize("change,message", [
        ({"ticker": "BBB_US_EQ"}, "ticker 'BBB_US_EQ' differs from instrument.ticker 'AAA_US_EQ'"),
        ({"quantity": None}, "a QUANTITY order without quantity"),
        ({"strategy": "VALUE"}, "a VALUE order without value"),
        ({"type": "LIMIT"}, "a LIMIT order without limitPrice"),
        ({"type": "STOP_LIMIT", "limitPrice": 10.0}, "a STOP_LIMIT order without stopPrice"),
        ({"timeInForce": "FOREVER"}, r"\$\.timeInForce: 'FOREVER' is not a documented value"),
    ])
    def test_fields_that_contradict_the_order_type_or_strategy_are_refused(self, change, message):
        with pytest.raises(S.BadValue, match=message):
            R.parse_order({**F.order(), **change})

    def test_a_limit_order_with_its_price_and_time_in_force_is_read(self):
        o = R.parse_order({**F.order(), "type": "LIMIT", "limitPrice": 49.5, "timeInForce": "DAY"})
        assert (o.limit_price_order_ccy, o.time_in_force) == (49.5, "DAY")

    def test_the_pending_orders_read_is_a_list_of_orders(self):
        assert [o.order_id for o in R.parse_orders([F.order(1), F.order(2)])] == [1, 2]


class TestHistory:
    def test_a_historical_order_page_carries_the_fill_its_fees_and_the_next_page(self):
        page = R.parse_historical_orders_page(F.page([F.historical_order(quantity=4.0, price=50.25, fx_rate=0.79)], "/api/v0/equity/history/orders?cursor=5&limit=50"))
        (h,) = page.items
        assert page.next_page_path == "/api/v0/equity/history/orders?cursor=5&limit=50"
        assert h.fill is not None and (h.fill.price_instrument_ccy, h.fill.quantity_shares, h.fill.wallet_impact.fx_rate) == (50.25, 4.0, 0.79)
        assert [(t.name, t.amount, t.currency) for t in h.fill.wallet_impact.taxes] == [("CURRENCY_CONVERSION_FEE", 0.6, "GBP")]

    def test_an_order_that_never_filled_has_no_fill(self):
        raw = {"order": F.order(status="CANCELLED"), "fill": None}
        assert R.parse_historical_orders_page(F.page([raw])).items[0].fill is None

    def test_a_page_without_its_next_page_field_is_refused_even_on_the_last_page(self):
        with pytest.raises(S.MissingField, match=r"\$\.nextPagePath: required field is absent"):
            R.parse_transactions_page({"items": []})

    def test_dividends_and_transactions_are_read_with_their_currencies(self):
        d = R.parse_dividends_page(F.page([F.dividend()])).items[0]
        t = R.parse_transactions_page(F.page([F.transaction("FEE", -1.2)])).items[0]
        assert (d.dividend_type, d.amount_account_ccy, d.account_currency, d.ticker_currency) == ("ORDINARY", 1.63, "GBP", "USD")
        assert (t.transaction_type, t.amount, t.currency) == ("FEE", -1.2, "GBP") and t.at_utc.tzinfo is dt.UTC

    def test_a_dividend_whose_ticker_differs_from_its_instrument_is_refused(self):
        with pytest.raises(S.BadValue, match="differs from instrument.ticker"):
            R.parse_dividends_page(F.page([{**F.dividend(), "ticker": "ZZZ_US_EQ"}]))

    def test_every_documented_dividend_type_is_accepted_and_others_refused(self):
        assert len(R.DIVIDEND_TYPES) == 58 and "BONUS_MANUFACTURED_PAYMENT" in R.DIVIDEND_TYPES
        with pytest.raises(S.BadValue, match="58 in all"):
            R.parse_dividends_page(F.page([{**F.dividend(), "type": "SPECIAL"}]))


class TestMetadata:
    def test_exchanges_carry_their_working_schedules(self):
        (e,) = R.parse_exchanges([F.exchange()])
        assert e.name == "NASDAQ" and [ev.event_type for ev in e.working_schedules[0].time_events] == ["OPEN", "CLOSE"]

    def test_instruments_are_read_with_the_documented_fields_only(self):
        (i,) = R.parse_instruments([F.tradable_instrument("BBB_US_EQ")])
        assert (i.ticker, i.instrument_type, i.currency, i.working_schedule_id) == ("BBB_US_EQ", "STOCK", "USD", 53)
        with pytest.raises(S.UnexpectedField, match=r"\['minTradeQuantity'\]"):
            R.parse_instruments([{**F.tradable_instrument(), "minTradeQuantity": 0.01}])

    def test_a_finished_export_needs_its_download_link_and_an_unfinished_one_may_lack_it(self):
        assert R.parse_exports([F.export_report("Queued")])[0].download_link is None
        with pytest.raises(S.BadValue, match="a Finished report without downloadLink"):
            R.parse_exports([{**F.export_report("Finished"), "downloadLink": None}])
