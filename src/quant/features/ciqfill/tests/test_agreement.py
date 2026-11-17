"""quant.features.ciqfill.agreement: the share of rows on which a rebuilt value agrees with the engine's, as every replication gate measures it."""
from __future__ import annotations

import pandas as pd

from quant.features.ciqfill.agreement import equal_share


def test_values_agree_within_the_relative_tolerance_and_near_zero_within_the_absolute_one():
    e = pd.Series([2.0, 3.0, 5.0, 0.0, 0.0, 0.0, None])
    r = pd.Series([2.0 + 1e-13, 3.0 * (1 + 1e-7), 5.5, 1e-13, 1e-10, 0.0, 7.0])
    both = e.notna() & r.notna()
    assert equal_share(e, r, both, dates=False, atol=1e-12) == round(4 / 6, 4)          # near zero only the absolute tolerance counts: 1e-10 is outside 1e-12
    assert equal_share(e, r, both, dates=False, atol=1e-9) == round(5 / 6, 4)           # ... and inside 1e-9; 5.5 against 5.0 never agrees


def test_knowledge_dates_agree_only_on_the_same_day():
    e = pd.Series(pd.to_datetime(["2026-05-01", "2026-05-02", "2026-05-03"]))
    r = pd.Series(pd.to_datetime(["2026-05-01", "2026-05-03", "2026-05-03"]))
    assert equal_share(e, r, pd.Series([True, True, True]), dates=True, atol=0.0) == round(2 / 3, 4)


def test_with_no_row_on_both_sides_there_is_no_share():
    assert equal_share(pd.Series([1.0]), pd.Series([None]), pd.Series([False]), dates=False, atol=1e-9) is None
