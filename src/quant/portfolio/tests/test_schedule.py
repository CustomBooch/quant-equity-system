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


def test_the_rotation_keeps_every_2026_slot_and_does_not_repeat_across_a_53_week_year():
    tue = dt.date(2026, 1, 6)
    for i in range(51):                                                              # A33 keeps the A5 tranche of every slot through 2026-12-22
        d = tue + dt.timedelta(weeks=i)
        assert S.tranche_of(d) == d.isocalendar().week % 4
    ids = [S.tranche_of(dt.date(2026, 12, 22) + dt.timedelta(weeks=i)) for i in range(5)]
    assert ids == [0, 1, 2, 3, 0]                                                    # ISO weeks 52, 53, 1, 2, 3 would give 0, 1, 1, 2, 3


def test_overlay_exposure_is_the_defensive_level_below_trend_and_full_otherwise():
    assert S.overlay_exposure(90.0, 100.0) == 0.5 and S.overlay_exposure(110.0, 100.0) == 1.0
    assert S.overlay_exposure(90.0, 100.0, level=0.75) == 0.75 and S.SHADOW_LEVELS == (0.75, 0.25)


def test_tranche_targets_use_a_quarter_of_the_account_times_exposure():
    t = S.tranche_target_shares({"A": 0.6, "B": 0.4}, {"A": 10.0, "B": 20.0}, 10000.0, 0.5)   # exposure passed explicitly
    assert t == {"A": 75.0, "B": 25.0}                               # 10000/4*0.5 = 1250; 0.6*1250/10, 0.4*1250/20


def test_slot_trade_is_the_change_of_that_tranche_only():
    old = {"A": 10.0}
    new = S.tranche_target_shares({"A": 0.5, "B": 0.5}, {"A": 50.0, "B": 50.0}, 10000.0, 1.0)
    p = P.make_plan(new, old, {"A": 50.0, "B": 50.0}, 10000.0)
    assert {o["ticker"]: o["quantity"] for o in p["orders"]} == {"A": 15.0, "B": 25.0}


def test_overlay_refuses_missing_market_data_instead_of_defaulting_to_full_exposure():
    for close, mean in ((float("nan"), 100.0), (90.0, float("nan")), (float("inf"), 100.0)):
        with pytest.raises(ValueError, match="finite"):
            S.overlay_exposure(close, mean)


def test_only_the_slots_own_tranche_is_plannable_except_on_an_amended_catch_up_day():
    assert S.plannable(dt.date(2026, 12, 1), S.tranche_of(dt.date(2026, 12, 1))) and not S.plannable(dt.date(2026, 12, 1), 3)
    assert all(S.plannable(dt.date(2026, 11, 24), i) for i in range(4))           # A30: the whole account moves to the model on 2026-11-24
