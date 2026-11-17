"""quant.data.capiq.ciq: one monthly Capital IQ fundamentals export is read with its units, dates and flags, and every old or broken layout is refused."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.data.capiq.ciq import CiqFormatError, load_month
from quant.features.ciqfill.tests.factories import MNEM, SERIAL, company_rows, header, make_file, write_xlsx


def test_good_file_loads_with_units_dates_and_flags(tmp_path):
    rows = company_rows(3)
    rows[1][4] = "NA"
    rows[2][12] = SERIAL("2026-07-01")                  # filed after the 06/30 pricing date
    m = load_month(make_file(tmp_path / "good.xlsx", rows))
    t = m.table
    assert m.pricing_date == pd.Timestamp("2026-06-30") and m.asof_date == pd.Timestamp("2026-07-01") and len(t) == 3
    assert t["mcap"].iloc[0] == pytest.approx(rows[0][13] * 1e6) and t["revenue"].iloc[0] == pytest.approx(rows[0][4] * 1e3)    # $M and $000 -> $
    assert np.isnan(t["revenue"].iloc[1]) and t["cusip8"].iloc[0] == "C0000000" and t["exch"].iloc[0] == "NYSE"
    assert list(t["late_filing"]) == [False, False, True]


@pytest.mark.parametrize("kw,msg", [
    ({"period": "Latest Fiscal Year"}, "expected 'Last Twelve Months'"),
    ({"asof": "07/05/2026"}, "day after the pricing date"),
    ({"mnem": MNEM[:11] + ["340263"] + MNEM[12:]}, "mnemonic '340263'"),
])
def test_old_or_broken_formats_are_refused(tmp_path, kw, msg):
    with pytest.raises(CiqFormatError, match=msg):
        load_month(make_file(tmp_path / "bad.xlsx", **kw))


def test_different_pricing_dates_and_a_missing_column_are_refused(tmp_path):
    h = header()
    h[4][14] = "07/31/2026"                              # close price priced on another date
    write_xlsx(tmp_path / "a.xlsx", h + company_rows(3))
    with pytest.raises(CiqFormatError, match="different pricing dates"):
        load_month(tmp_path / "a.xlsx")
    h = header()
    h[2][15] = "Float"
    write_xlsx(tmp_path / "b.xlsx", h + company_rows(3))
    with pytest.raises(CiqFormatError, match="missing column 'Shares Outstanding'"):
        load_month(tmp_path / "b.xlsx")


def test_a_fundamentals_as_of_cell_that_cannot_be_read_is_refused_as_a_format_error(tmp_path):
    h = header()
    h[5][4] = None                                       # Total Revenue's as-of cell empty, the other three say 07/01/2026
    write_xlsx(tmp_path / "a.xlsx", h + company_rows(3))
    with pytest.raises(CiqFormatError, match=r"fundamentals as-of \['2026-07-01', 'unreadable'\]"):      # was a TypeError from sorting None with dates
        load_month(tmp_path / "a.xlsx")
