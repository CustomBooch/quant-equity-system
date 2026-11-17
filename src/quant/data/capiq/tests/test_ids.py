"""quant.data.capiq.ids: Capital IQ companies are matched to panel stocks by CUSIP first, then ticker, keeping the largest share class."""
from __future__ import annotations

import pandas as pd

from quant.data.capiq.ciq import load_month
from quant.data.capiq.ids import cusip8, match_permno, ticker_norm
from quant.features.ciqfill.tests.factories import company_rows, make_file


def test_matching_prefers_cusip_falls_back_to_ticker_and_keeps_the_largest_class(tmp_path):
    t = load_month(make_file(tmp_path / "m.xlsx", company_rows(3))).table
    state = pd.DataFrame({"permno": [10, 11, 12, 13], "cusip": ["C0000000", "C0000001", None, "C0000001"], "ticker": ["XX", "T01", "T02", "ZZ"], "me": [5.0, 1.0, 2.0, 9.0]})
    m = match_permno(t, state)
    assert m["permno"].tolist() == [10, 13, 12] and m["match_how"].tolist() == ["cusip", "cusip", "ticker"]   # permno 13 is the larger class of C0000001


def test_a_cusip_is_matched_on_its_first_eight_characters_in_upper_case_and_a_missing_one_stays_missing():
    assert cusip8(pd.Series(["037833100", "g7945m107", None, "1234"])).tolist() == ["03783310", "G7945M10", pd.NA, "1234"]


def test_tickers_match_whatever_their_case_and_dots():
    assert ticker_norm(pd.Series(["brk.b", "BRKB", "B.F.A", None])).tolist() == ["BRKB", "BRKB", "BFA", pd.NA]
