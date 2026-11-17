"""quant.portfolio.plan: the orders a slot should trade, the plan record the engine requires, and the prices plans use. Boundary replaced: Yahoo (yfinance);
the files go to tmp_path; urlopen is replaced only to prove that planning never calls it."""
from __future__ import annotations

import datetime as dt
import json
import urllib.request

import pandas as pd
import pytest

from quant.execution import audit as A
from quant.portfolio import plan as P

PRICES = {"AAA": 50.0, "BBB": 20.0}


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PLANS", tmp_path / "plans")
    monkeypatch.setattr(A, "PATH", tmp_path / "audit.jsonl")


@pytest.fixture()
def no_network(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: calls.append(req.full_url))
    return calls


def plan(**kw):
    """Buy 10 AAA from nothing, sell the 4 BBB held: 500 and 80 USD on a 10,000 USD account."""
    return P.make_plan({"AAA": 10.0, "BBB": 0.0}, {"BBB": 4.0}, PRICES, 10000.0, **kw)


class TestMakePlan:
    def test_buys_sells_and_skips_orders_below_the_minimum(self):
        p = P.make_plan({"A": 1.0, "B": 0.0, "C": 0.01}, {"B": 2.0}, {"A": 100.0, "B": 10.0, "C": 100.0}, 10000.0)
        assert {o["ticker"]: (o["side"], o["quantity"]) for o in p["orders"]} == {"A": ("BUY", 1.0), "B": ("SELL", -2.0)}
        assert p["skipped"] == [{"ticker": "C", "reason": "below_min_order", "usd": 1.0}]

    def test_a_tranche_fraction_moves_only_that_fraction_of_the_way(self):
        assert {o["ticker"]: o["quantity"] for o in plan(tranche_fraction=0.25)["orders"]} == {"AAA": 2.5, "BBB": -1.0}

    def test_a_name_without_a_price_is_skipped_not_guessed(self):
        p = P.make_plan({"A": 1.0}, {}, {}, 10000.0)
        assert p["orders"] == [] and p["skipped"] == [{"ticker": "A", "reason": "no_price"}]

    def test_the_hash_changes_with_any_order_and_with_the_account_value(self):
        assert plan()["plan_hash"] != P.make_plan({"AAA": 11.0}, {"BBB": 4.0}, PRICES, 10000.0)["plan_hash"]
        assert plan()["plan_hash"] != P.make_plan({"AAA": 10.0}, {"BBB": 4.0}, PRICES, 10001.0)["plan_hash"]

    def test_writing_a_plan_stores_it_under_its_hash_and_records_it(self):
        p = plan()
        f = P.write_plan(p)
        assert f == P.PLANS / f"{p['plan_hash']}.json" and json.loads(f.read_text()) == p
        assert A.records()[0] | {"utc": None, "prev": None} == {"utc": None, "event": "plan", "plan_hash": p["plan_hash"], "file": str(f), "orders": 2, "slot": None, "tranche": None, "broker": "",
                                                                    "plan_sha256": P.plan_seal(p), "prev": None}


def test_latest_prices_carry_the_last_close_forward_and_drop_non_positive_prices(yahoo):
    prices, day = P.latest_prices(["AAA", "BBB", "DEAD"])
    assert prices == {"AAA": 50.0, "BBB": 20.0} and day == "2026-10-23"


def test_a_plan_from_a_target_book_buys_one_tranche_and_reads_nothing_from_the_broker(tmp_path, yahoo, no_network):
    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [0.6, 0.4]}).to_csv(f, index=False)
    p = P.plan_from_target(f, 10000.0, 0.5, dt.date(2026, 10, 27))
    assert {o["ticker"]: o["quantity"] for o in p["orders"]} == {"AAA": 15.0, "BBB": 25.0}     # 10000 / 4 tranches * 0.5 exposure = 1250: 750 / 50 and 500 / 20
    assert p["tranche"] == dt.date(2026, 10, 27).isocalendar().week % 4 and p["held_source"].startswith("assumed empty") and p["price_date"] == "2026-10-23"
    assert no_network == []                                                                # planning never calls the broker


def test_a_plan_from_an_alpaca_book_names_its_broker_and_trades_plain_symbols(tmp_path, yahoo, no_network):
    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [0.6, 0.4]}).to_csv(f, index=False)
    p = P.plan_from_target(f, 10000.0, 0.5, dt.date(2026, 10, 27))
    assert p["broker"] == "alpaca" and {o["ticker"] for o in p["orders"]} == {"AAA", "BBB"}


def test_a_book_that_prices_a_symbol_from_another_ticker_is_refused(tmp_path, yahoo, no_network):
    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA", "BRK.B"], "ticker": ["BBB", "BRK-B"], "w": [0.5, 0.5]}).to_csv(f, index=False)    # BRK.B / BRK-B is one security
    with pytest.raises(ValueError, match=r"different Yahoo tickers .*\['AAA->BBB'\]"):
        P.plan_from_target(f, 10000.0, 0.5, dt.date(2026, 10, 27))


def test_a_book_without_a_column_of_alpaca_symbols_is_refused(tmp_path, yahoo, no_network):
    f = tmp_path / "book.csv"
    pd.DataFrame({"symbol": ["AAA"], "ticker": ["AAA"], "w": [1.0]}).to_csv(f, index=False)
    with pytest.raises(ValueError, match="no `line` column"):
        P.plan_from_target(f, 10000.0, 0.5, dt.date(2026, 10, 27))


def test_short_lines_of_an_a11_book_become_whole_share_sells_and_the_plan_declares_s(tmp_path, yahoo, no_network):
    import json

    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [1.2, -0.2], "side": ["long", "short"]}).to_csv(f, index=False)
    f.with_suffix(".json").write_text(json.dumps({"short_fraction": 0.2, "overlay_state": {"exposure_now": 1.0}}))
    p = P.plan_from_target(f, 10000.0, 1.0, dt.date(2026, 10, 27))
    q = {o["ticker"]: o["quantity"] for o in p["orders"]}
    assert p["short_fraction"] == 0.2 and q["AAA"] == pytest.approx(60.0) and q["BBB"] == -25.0       # 2500 x 1.2 / 50; 2500 x 0.2 / 20 = 25 whole shares


def test_a_levered_a12_book_scales_both_legs_by_k_and_the_plan_declares_it(tmp_path, yahoo, no_network):
    import json

    k = 2.0 / 1.4
    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [1.2 * k, -0.2 * k], "side": ["long", "short"]}).to_csv(f, index=False)
    f.with_suffix(".json").write_text(json.dumps({"short_fraction": 0.2, "leverage": k, "overlay_state": {"exposure_now": 1.0}}))
    p = P.plan_from_target(f, 10000.0, 1.0, dt.date(2026, 10, 27))
    q = {o["ticker"]: o["quantity"] for o in p["orders"]}
    assert p["leverage"] == pytest.approx(k) and q["AAA"] == pytest.approx(round(2500 * 1.2 * k / 50, 4)) and q["BBB"] == -35.0   # 2500 x 0.2857 / 20 = 35.7 -> 35


def test_an_unlevered_book_gives_a_plan_without_a_leverage_field(tmp_path, yahoo, no_network):
    import json

    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA", "BBB"], "ticker": ["AAA", "BBB"], "w": [1.2, -0.2], "side": ["long", "short"]}).to_csv(f, index=False)
    f.with_suffix(".json").write_text(json.dumps({"short_fraction": 0.2, "overlay_state": {"exposure_now": 1.0}}))
    assert "leverage" not in P.plan_from_target(f, 10000.0, 1.0, dt.date(2026, 10, 27))


def test_a_name_the_tranche_holds_but_the_new_book_dropped_is_sold_not_silently_kept(tmp_path, yahoo, no_network):
    """Amendment A16: held names are priced too, so one that left the book is sold (it used to be skipped as `no_price` and kept for ever)."""
    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA"], "ticker": ["AAA"], "w": [1.0]}).to_csv(f, index=False)
    p = P.plan_from_target(f, 10000.0, 0.5, dt.date(2026, 10, 27), held={"BBB": 4.0})
    sells = {o["ticker"]: o["quantity"] for o in p["orders"] if o["side"] == "SELL"}
    assert sells == {"BBB": -4.0} and not [s for s in p["skipped"] if s["reason"] == "no_price"]


def test_a_dropped_short_is_covered_with_a_buy(tmp_path, yahoo, no_network):
    f = tmp_path / "book.csv"
    pd.DataFrame({"line": ["AAA"], "ticker": ["AAA"], "w": [1.0]}).to_csv(f, index=False)
    p = P.plan_from_target(f, 10000.0, 0.5, dt.date(2026, 10, 27), held={"BBB": -3.0})
    assert {o["ticker"]: o["quantity"] for o in p["orders"] if o["ticker"] == "BBB"} == {"BBB": 3.0}
