"""quant.features.ciqfill.sec13f: which 13F reports make up a manager's quarter, the one-quarter carry-forward and change, and the engine's ownership formulas, on small
SEC-format data-set zips written to tmp_path."""
from __future__ import annotations

import zipfile

import numpy as np
import pandas as pd
import pytest

from quant.features.ciqfill import sec13f as S

Q4, Q1, Q2 = "31-DEC-2025", "31-MAR-2026", "30-JUN-2026"


def dataset(path, subs, holds):
    """`subs`: (accession, cik, period, filed, type, amendment type); `holds`: (accession, cusip, shares, sh/prn, put/call)."""
    sub = "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n" + "".join(f"{a}\t{f}\t{t}\t{c}\t{p}\n" for a, c, p, f, t, _ in subs)
    cov = "ACCESSION_NUMBER\tAMENDMENTTYPE\tREPORTTYPE\n" + "".join(f"{a}\t{at}\t13F HOLDINGS REPORT\n" for a, _, _, _, _, at in subs)
    inf = "ACCESSION_NUMBER\tINFOTABLE_SK\tCUSIP\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL\n" + "".join(f"{a}\t{i}\t{c}\t{s}\t{t}\t{pc}\n" for i, (a, c, s, t, pc) in enumerate(holds))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SUBMISSION.tsv", sub)
        z.writestr("COVERPAGE.tsv", cov)
        z.writestr("INFOTABLE.tsv", inf)
    return path


def reports(tmp_path, subs, holds):
    d = S.extract(dataset(tmp_path / "x_form13f.zip", subs, holds))
    import duckdb
    return S.reports([d], duckdb.connect()), d


class TestSelect:
    def test_a_restatement_replaces_the_original_and_new_holdings_are_added(self, tmp_path):
        subs = [("a1", 7, Q1, "15-MAY-2026", "13F-HR", ""), ("a2", 7, Q1, "20-MAY-2026", "13F-HR/A", "RESTATEMENT"),
                ("a3", 7, Q1, "25-MAY-2026", "13F-HR/A", "NEW HOLDINGS"), ("a4", 7, Q1, "10-MAY-2026", "13F-HR/A", "NEW HOLDINGS")]
        r, _ = reports(tmp_path, subs, [])
        assert sorted(S.select(r)["accession"]) == ["a2", "a3"]                    # a4 was filed before the restatement it would add to

    def test_of_two_originals_the_later_one_counts(self, tmp_path):
        r, _ = reports(tmp_path, [("a1", 7, Q1, "15-MAY-2026", "13F-HR", ""), ("a2", 7, Q1, "16-MAY-2026", "13F-HR", "")], [])
        assert S.select(r)["accession"].tolist() == ["a2"]


class TestHoldings:
    def test_only_share_rows_without_options_are_summed_by_cusip8(self, tmp_path):
        subs = [("a1", 7, Q1, "15-MAY-2026", "13F-HR", "")]
        holds = [("a1", "123456789", 100, "SH", ""), ("a1", "123456780", 50, "SH", ""), ("a1", "123456789", 999, "PRN", ""), ("a1", "123456789", 777, "SH", "Put")]
        r, d = reports(tmp_path, subs, holds)
        import duckdb
        h = S.holdings([d], S.select(r), duckdb.connect())
        assert h["sh"].tolist() == [150] and h["cusip"].tolist() == ["12345678"]

    def test_holdings_outside_the_kept_cusips_are_dropped(self, tmp_path):
        subs = [("a1", 7, Q1, "15-MAY-2026", "13F-HR", "")]
        r, d = reports(tmp_path, subs, [("a1", "123456789", 100, "SH", ""), ("a1", "999999999", 50, "SH", "")])
        import duckdb
        h = S.holdings([d], S.select(r), duckdb.connect(), keep={"99999999"})
        assert h["cusip"].tolist() == ["99999999"]


class TestStaleAndChange:
    def test_a_missing_manager_is_carried_one_quarter_with_no_change_and_the_others_get_their_change(self):
        h = pd.DataFrame({"cik": [1, 2, 1, 1], "period": pd.to_datetime(["2025-12-31", "2025-12-31", "2026-03-31", "2026-06-30"]),
                          "cusip": ["C"] * 4, "sh": [100, 40, 130, 90]})
        out = S.with_stale_and_change(h)
        q1 = out[out["period"] == pd.Timestamp("2026-03-31")].set_index("cik")
        assert q1.loc[1, "chg"] == 30 and q1.loc[2, "chg"] == 0 and q1.loc[2, "rdate"] == pd.Timestamp("2025-12-31")
        q2 = out[out["period"] == pd.Timestamp("2026-06-30")]
        assert q2["cik"].tolist() == [1]                                           # manager 2 is not carried a second quarter

    def test_a_new_position_counts_its_whole_holding_as_change(self):
        h = pd.DataFrame({"cik": [1, 1], "period": pd.to_datetime(["2025-12-31", "2026-03-31"]), "cusip": ["A", "B"], "sh": [10, 25]})
        out = S.with_stale_and_change(h)
        assert out.loc[(out["period"] == pd.Timestamp("2026-03-31")) & (out["cusip"] == "B"), "chg"].item() == 25


class TestCompleteQuarters:
    def test_a_period_with_few_managers_or_off_a_quarter_end_is_not_a_quarter(self):
        h = pd.DataFrame({"cik": [1, 2, 3, 1, 1], "period": pd.to_datetime(["2026-03-31"] * 3 + ["2023-06-30", "2026-05-15"])})
        assert S.complete_quarters(h, min_managers=2) == [pd.Timestamp("2026-03-31")]

    def test_nothing_is_carried_or_compared_across_a_missing_quarter(self):
        h = pd.DataFrame({"cik": [1, 2, 1], "period": pd.to_datetime(["2025-09-30", "2025-09-30", "2026-03-31"]), "cusip": ["C"] * 3, "sh": [10, 20, 15]})
        out = S.with_stale_and_change(h)
        q1 = out[out["period"] == pd.Timestamp("2026-03-31")]
        assert q1["cik"].tolist() == [1] and np.isnan(q1["chg"].item())


class TestVintages:
    def test_ownership_ratios_follow_the_engine_formulas(self, tmp_path):
        subs = [("q4a", 1, Q4, "10-FEB-2026", "13F-HR", ""), ("q4b", 2, Q4, "10-FEB-2026", "13F-HR", ""),
                ("q1a", 1, Q1, "10-MAY-2026", "13F-HR", ""), ("q1c", 3, Q1, "10-MAY-2026", "13F-HR", "")]
        holds = [("q4a", "11111111X", 100, "SH", ""), ("q4b", "11111111X", 50, "SH", ""), ("q1a", "11111111X", 150, "SH", ""), ("q1c", "11111111X", 300, "SH", "")]
        f13 = tmp_path / "f13"
        f13.mkdir()
        dataset(f13 / "01dec2025-31may2026_form13f.zip", subs, holds)
        state = pd.DataFrame({"permno": [9, 9], "smonth": pd.to_datetime(["2025-12-01", "2026-03-01"]), "shares": [1000.0, 1000.0]})
        v = S.vintages(f13, lambda k: pd.Series(9, index=k.index), state, min_managers=1)
        r = v.set_index("fdate").loc[pd.Timestamp("2026-03-31")]
        # Q1: manager 1 holds 150 (change +50), manager 2 is carried at 50 (stale, change 0), manager 3 is new with 300 (change +300)
        assert r["io_n_mgr"] == 3 and r["io_frac"] == pytest.approx(0.5)
        assert r["io_hhi"] == pytest.approx((150**2 + 50**2 + 300**2) / 500**2)
        assert r["io_stale_frac"] == pytest.approx(50 / 500)
        assert r["io_buy_sell"] == pytest.approx(1.0) and r["io_net_change"] == pytest.approx(350 / 1000)
        assert np.isnan(r["io_frac_chg"])                                          # the first quarter in the data is no vintage, so no previous one
        assert r["knowledge_date"] == pd.Timestamp("2026-05-30")


class TestAsof:
    def test_the_latest_vintage_known_before_the_month_is_used_within_120_days(self):
        v = pd.DataFrame({"permno": [1, 1, 2], "fdate": pd.to_datetime(["2026-03-31", "2026-06-30", "2026-03-31"]),
                          "knowledge_date": pd.to_datetime(["2026-05-30", "2026-08-29", "2026-05-30"]), **{c: [1.0, 2.0, 3.0] for c in S.IO_COLS}})
        a = S.asof(v, pd.Series([1, 2]), pd.Timestamp("2026-10-01")).set_index("permno")
        assert a.loc[1, "io_frac"] == 2.0 and np.isnan(a.loc[2, "io_frac"])         # stock 2's last vintage is 124 days old
