import datetime as dt

import pytest

from quant.portfolio import plan as P
from quant.portfolio import schedule as S


def test_only_tuesdays_are_slots_and_tranches_rotate_over_four_weeks():
    tue = dt.date(2026, 10, 20)
    assert S.is_slot(tue) and not S.is_slot(dt.date(2026, 10, 21))
    ids = [S.tranche_of(tue + dt.timedelta(weeks=i)) for i in range(8)]
    assert sorted(ids[:4]) == [0, 1, 2, 3] and ids[:4] == ids[4:]
    with pytest.raises(ValueError):
        S.tranche_of(dt.date(2026, 10, 21))


def test_overlay_exposure_is_the_defensive_level_below_trend_and_full_otherwise():
    assert S.overlay_exposure(90.0, 100.0) == 0.5 and S.overlay_exposure(110.0, 100.0) == 1.0
    assert S.overlay_exposure(90.0, 100.0, level=0.75) == 0.75 and S.SHADOW_LEVELS == (0.75, 0.25)


def test_tranche_targets_use_a_quarter_of_the_account_times_exposure():
    t = S.tranche_target_shares({"A_US_EQ": 0.6, "B_US_EQ": 0.4}, {"A_US_EQ": 10.0, "B_US_EQ": 20.0}, 10000.0, 0.5)   # exposure passed explicitly
    assert t == {"A_US_EQ": 75.0, "B_US_EQ": 25.0}                               # 10000/4*0.5 = 1250; 0.6*1250/10, 0.4*1250/20


def test_slot_trade_is_the_change_of_that_tranche_only():
    old = {"A_US_EQ": 10.0}
    new = S.tranche_target_shares({"A_US_EQ": 0.5, "B_US_EQ": 0.5}, {"A_US_EQ": 50.0, "B_US_EQ": 50.0}, 10000.0, 1.0)
    p = P.make_plan(new, old, {"A_US_EQ": 50.0, "B_US_EQ": 50.0}, 10000.0)
    assert {o["ticker"]: o["quantity"] for o in p["orders"]} == {"A_US_EQ": 15.0, "B_US_EQ": 25.0}
