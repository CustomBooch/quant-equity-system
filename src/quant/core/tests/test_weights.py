"""quant.core.weights: capped weights keep proportions below the cap, hand the excess to uncapped names, and sum to one."""
import numpy as np
import pytest

from quant.core.weights import capped_weights


def test_weights_below_the_cap_are_proportional_to_the_input():
    w = capped_weights(np.array([1.0, 2.0, 3.0, 4.0]), cap=0.5)
    assert w.tolist() == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_a_capped_name_hands_its_excess_to_the_others_in_proportion():
    w = capped_weights(np.array([10.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]), cap=0.2)
    assert w[0] == pytest.approx(0.2) and w[1:].tolist() == pytest.approx([0.08] * 10) and w.sum() == pytest.approx(1.0)


def test_excess_handed_on_can_cap_a_second_name():
    w = capped_weights(np.array([50.0, 30.0, 10.0, 10.0]), cap=0.35)
    assert w.max() <= 0.35 + 1e-12 and w.sum() == pytest.approx(1.0) and w[2] == pytest.approx(w[3])


def test_with_fewer_names_than_one_over_the_cap_every_name_ends_at_the_cap():
    w = capped_weights(np.array([5.0, 1.0, 1.0]), cap=0.05)
    assert w.tolist() == pytest.approx([0.05, 0.05, 0.05])


def test_the_cap_the_books_pass_for_a_small_basket_gives_equal_weights():
    me = np.array([9.0, 4.0, 1.0, 1.0])
    assert capped_weights(me, max(0.05, 1 / len(me))).tolist() == pytest.approx([0.25] * 4)
