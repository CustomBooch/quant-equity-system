"""quant.features.ciqfill.ciq_bridge: the monthly Capital IQ bridge export (format refusals, CUSIP matching) and the surprise and consensus rules of SPEC_OCT_FILL.md H and I."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.features.ciqfill import ciq_bridge as CB
from quant.data.capiq.ciq import CiqFormatError
from quant.features.ciqfill.tests.factories import write_xlsx

COLS = ["SP_CUSIP"] + CB.REQUIRED


def bridge_file(path, companies, asof="10/01/2026", asof_of=None, drop=()):
    """A bridge export: header, mnemonic, period and as-of rows, then one row per company (dict of REQUIRED key -> value, plus 'cusip')."""
    cols = [c for c in COLS if c not in drop]
    rows = [[None], ["Entity Name"] + cols, ["SP_ENTITY_NAME"] + [c.split("|")[0] for c in cols],
            [None] + [c.split("|")[1] if "|" in c else None for c in cols], [None] + [None if c == "SP_CUSIP" else (asof_of or {}).get(c, asof) for c in cols]]
    for i, co in enumerate(companies):
        rows.append([f"Co {i}"] + [co.get("cusip") if c == "SP_CUSIP" else co.get(c, "NA") for c in cols])
    write_xlsx(path, rows)
    return path


def company(cusip, **kw):
    base = {"cusip": cusip, **{k: 1.0 for k in CB.REQUIRED}}
    base.update({k.replace("__", "|"): v for k, v in kw.items()})
    return base


class TestLoadBridge:
    def test_a_file_reads_one_row_per_company_with_numbers_and_na_as_missing(self, tmp_path):
        f = CB.load_bridge(bridge_file(tmp_path / "b.xlsx", [company("12345678X", **{"SP_EPS_EST__FY+1": 2.5}), company("87654321Y", **{"SP_EPS_EST__FY+1": "NA"})]))
        assert f.asof == pd.Timestamp("2026-10-01")
        assert f.table["cusip8"].tolist() == ["12345678", "87654321"]
        assert f.table["SP_EPS_EST|FY+1"].iloc[0] == 2.5 and np.isnan(f.table["SP_EPS_EST|FY+1"].iloc[1])

    def test_a_missing_required_column_is_refused(self, tmp_path):
        with pytest.raises(CiqFormatError, match=r"SP_EPS_STDDEV_EST\|FQ\+1"):
            CB.load_bridge(bridge_file(tmp_path / "b.xlsx", [company("12345678X")], drop=("SP_EPS_STDDEV_EST|FQ+1",)))

    def test_a_file_without_the_fy1_mean_takes_it_from_the_estimates_file_by_cusip8(self, tmp_path):
        p = bridge_file(tmp_path / "b.xlsx", [company("12345678X"), company("87654321Y")], drop=(CB.FY1,))
        f = CB.load_bridge(p, pd.Series([3.0], index=["12345678"]))
        assert f.table[CB.FY1].iloc[0] == 3.0 and np.isnan(f.table[CB.FY1].iloc[1])
        with pytest.raises(CiqFormatError, match=r"SP_EPS_EST\|FY\+1"):
            CB.load_bridge(p)                                                           # without the estimates the column is still required

    def test_columns_with_different_as_of_dates_are_refused(self, tmp_path):
        with pytest.raises(CiqFormatError, match="one as-of date"):
            CB.load_bridge(bridge_file(tmp_path / "b.xlsx", [company("12345678X")], asof_of={"SP_EST_ACT_REV|FQ0": "09/01/2026"}))

    def test_an_as_of_date_that_is_not_the_first_of_a_month_is_refused(self, tmp_path):
        with pytest.raises(CiqFormatError, match="not the first of a month"):
            CB.load_bridge(bridge_file(tmp_path / "b.xlsx", [company("12345678X")], asof="10/02/2026"))

    def test_a_workbook_without_the_entity_header_is_refused(self, tmp_path):
        write_xlsx(tmp_path / "b.xlsx", [["something else"]])
        with pytest.raises(CiqFormatError, match="Entity Name"):
            CB.load_bridge(tmp_path / "b.xlsx")


class TestByPermno:
    def test_a_cusip8_shared_by_two_share_classes_goes_to_the_larger_market_equity(self, tmp_path):
        f = CB.load_bridge(bridge_file(tmp_path / "b.xlsx", [company("12345678X")]))
        ids = pd.DataFrame({"permno": [1, 2], "cusip": ["12345678", "12345678"], "me": [5.0, 9.0]})
        assert CB.by_permno(f, ids).index.tolist() == [2]


def table(**cols):
    return pd.DataFrame({k.replace("__", "|"): v for k, v in cols.items()}, index=pd.Index([1, 2, 3], name="permno"))


M = pd.Timestamp("2026-10-01")


class TestSurpriseFeatures:
    def files(self, cur, earlier=None):
        f = {M: cur}
        if earlier is not None:
            f[pd.Timestamp("2026-08-01")] = earlier
        return f

    def cur(self, **kw):
        base = dict(SP_EST_ACT_EPS__FQ0=[1.2, 1.0, 1.0], SP_EST_EPS_DIFF__FQ0=[0.2, -0.5, 0.1], SP_EST_ACT_REV__FQ0=[110.0, 90.0, 100.0], SP_EST_REV_DIFF__FQ0=[10.0, -10.0, 0.0],
                    **{f"SP_EST_EPS_DIFF__FQ-{q}": [0.1, -0.1, np.nan] for q in (1, 2, 3)}, SP_EPS_STDDEV_EST__FQ1=[0.1, 0.25, 0.1])
        base.update(kw)
        return table(**{k.replace("FQ1", "FQ+1"): v for k, v in base.items()})

    def test_the_symmetric_surprise_uses_the_consensus_implied_by_actual_minus_difference(self):
        rdq = pd.DataFrame({"permno": [1, 2, 3], "rdq": pd.to_datetime(["2026-08-10"] * 3)})
        s = CB.surprise_features(self.files(self.cur()), rdq, M, pd.Index([1, 2, 3]))
        assert s.loc[1, "surprise_sym"] == pytest.approx(0.2 / (1.2 + 1.0))          # consensus 1.0
        assert s.loc[2, "sales_surprise_sym"] == pytest.approx(-10 / (90 + 100))

    def test_a_surprise_older_than_120_days_or_without_a_report_date_is_empty(self):
        rdq = pd.DataFrame({"permno": [1, 2], "rdq": pd.to_datetime(["2026-05-01", "2026-09-29"])})
        s = CB.surprise_features(self.files(self.cur()), rdq, M, pd.Index([1, 2, 3]))
        assert np.isnan(s.loc[1, "surprise_sym"])                                   # known 2026-05-02, 152 days before the month
        assert s.loc[2, "surprise_sym"] == pytest.approx(-0.5 / (1.0 + 1.5))
        assert np.isnan(s.loc[3, "surprise_sym"])                                   # no report date

    def test_a_report_on_the_last_day_before_the_month_is_not_yet_known(self):
        rdq = pd.DataFrame({"permno": [1], "rdq": pd.to_datetime(["2026-09-30"])})
        s = CB.surprise_features(self.files(self.cur()), rdq, M, pd.Index([1]))
        assert np.isnan(s.loc[1, "surprise_sym"])                                   # knowledge date 2026-10-01 is not before the month

    def test_the_four_quarter_count_needs_all_four_differences(self):
        rdq = pd.DataFrame({"permno": [1, 2, 3], "rdq": pd.to_datetime(["2026-08-10"] * 3)})
        s = CB.surprise_features(self.files(self.cur()), rdq, M, pd.Index([1, 2, 3]))
        assert s.loc[1, "pos_surprise_4q"] == 4 and s.loc[2, "pos_surprise_4q"] == 0
        assert np.isnan(s.loc[3, "pos_surprise_4q"])

    def test_dispersion_comes_from_the_earlier_file_only_if_no_other_report_falls_between(self):
        rdq = pd.DataFrame({"permno": [1, 2, 2], "rdq": pd.to_datetime(["2026-08-10", "2026-08-10", "2026-08-05"])})
        s = CB.surprise_features(self.files(self.cur(), earlier=self.cur(SP_EPS_STDDEV_EST__FQ1=[0.4, 0.5, 0.1])), rdq, M, pd.Index([1, 2]))
        assert s.loc[1, "surprise_disp"] == pytest.approx(0.2 / 0.4)                # the file of 2026-08-01, not the current one
        assert np.isnan(s.loc[2, "surprise_disp"])                                  # a second report after that file: its FQ+1 was another quarter

    def test_dispersion_is_empty_without_an_earlier_file(self):
        rdq = pd.DataFrame({"permno": [1], "rdq": pd.to_datetime(["2026-08-10"])})
        assert np.isnan(CB.surprise_features(self.files(self.cur()), rdq, M, pd.Index([1])).loc[1, "surprise_disp"])


class TestConsensusFeatures:
    def test_growth_and_revenue_revision_follow_the_engine_formulas(self):
        cur = table(SP_EPS_EST__FY_1=[2.0, 0.0, -1.0], SP_EPS_EST__FY_2=[3.0, 1.0, -0.5], SP_REV_EST__FY_1=[110.0, 50.0, 0.0])
        cur.columns = [c.replace("FY_", "FY+") for c in cur.columns]
        prev = cur.assign(**{"SP_REV_EST|FY+1": [100.0, 0.0, 10.0]})
        out = CB.consensus_features(cur, prev, pd.Index([1, 2, 3]))
        assert out["fy2_growth"].tolist()[0] == pytest.approx(0.5) and np.isnan(out.loc[2, "fy2_growth"])      # zero FY+1 mean
        assert out.loc[3, "fy2_growth"] == pytest.approx(0.5)                       # divided by |FY+1|
        assert out.loc[1, "rev_sal_fy1_1m"] == pytest.approx(0.1) and np.isnan(out.loc[2, "rev_sal_fy1_1m"])

    def test_without_an_earlier_file_the_revision_is_empty(self):
        cur = pd.DataFrame({"SP_EPS_EST|FY+1": [1.0], "SP_EPS_EST|FY+2": [1.1], "SP_REV_EST|FY+1": [5.0]}, index=pd.Index([1]))
        assert np.isnan(CB.consensus_features(cur, None, pd.Index([1])).loc[1, "rev_sal_fy1_1m"])
