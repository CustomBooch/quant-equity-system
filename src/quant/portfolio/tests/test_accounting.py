"""quant.portfolio.accounting: the retail terms of a month: cash earns rf, a debit beyond the short proceeds pays rf plus the spread, borrow on the short leg only."""
import pytest

from quant.portfolio import accounting as AC


def test_an_unlevered_long_only_month_is_the_long_return():
    assert AC.account_month(0.02, 0.0, 0.0, 1.0, 0.0, 0.0, 0.004) == pytest.approx(0.02)


def test_uninvested_cash_earns_the_risk_free_rate():
    assert AC.account_month(0.02, 0.0, 0.0, 0.5, 0.0, 0.0, 0.004) == pytest.approx(0.5 * 0.02 + 0.5 * 0.004)


def test_a_levered_long_book_pays_the_rate_plus_the_spread_on_the_debit_beyond_the_short_proceeds():
    r = AC.account_month(0.0, 0.0, 0.0, 1.7, 0.3, 0.0, 0.004)
    assert r == pytest.approx(-(1.7 - 1 - 0.3) * (0.004 + 0.025 / 12) - 0.3 * 0.005 / 12)


def test_a_short_that_rises_loses_and_borrow_is_charged_on_the_short_leg_not_the_hedge():
    r = AC.account_month(0.0, 0.10, 0.05, 1.0, 0.2, 0.1, 0.0)
    assert r == pytest.approx(-0.2 * 0.10 + 0.1 * 0.05 - 0.2 * AC.BORROW / 12)                 # longs plus hedge (1.1) are covered by equity plus proceeds (1.2)


def test_the_cost_is_taken_as_given():
    assert AC.account_month(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, cost=0.001) == pytest.approx(-0.001)


def test_the_sleeve_is_the_shorts_the_hedge_and_borrow_only():
    assert AC.sleeve_month(0.10, 0.05, 0.2, 0.1) == pytest.approx(-0.02 + 0.005 - 0.2 * AC.BORROW / 12)
