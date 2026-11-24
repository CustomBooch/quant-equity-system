"""quant.features.ciqfill.assemble end to end on synthetic engine builds and exports: the release it writes, its gates, and what it fills, refuses or leaves empty."""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import pytest

from quant.features.ciqfill import pit_families as pf
from quant.data.capiq.ciq import CiqFormatError
from quant.core.release import require_validation, verify_release
from quant.features.ciqfill.tests.factories import build_base, build_ciq, company_rows, est_rows, make_est_file, make_file, run


def test_assemble_adds_four_bridge_months_after_the_last_engine_month_and_publishes_a_release(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "master", pd.Timestamp("2026-06-01"))
    assert all(v == "pass" for v in rep["gates"].values()), rep["gates"]
    assert rep["gates"]["flags_replicate_engine"] == "pass" and rep["months"]["july"]["kind"] == "bridge" and rep["months"]["july"]["market_source"] == "crsp_state"
    assert rep["months"]["aug"]["market_source"] == "capital_iq" and rep["months"]["july"]["parity_vs_engine"]["reference_month"] == "2026-06-01"
    p = tmp_path / "master" / "master_panel.parquet"
    con = duckdb.connect()
    rows = {str(m): (n, b) for m, n, b in con.execute(f"select month, count(*), sum(case when bridge then 1 else 0 end) from read_parquet('{p.as_posix()}') group by 1 order by 1").fetchall()}
    assert list(rows) == ["2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01"]
    assert [rows[m][1] for m in rows] == [0, 0, 0, 60, 60, 60, 60]
    types = {r[0]: r[1] for r in con.execute(f"describe select permno, month from read_parquet('{p.as_posix()}')").fetchall()}
    assert types["permno"] == "INTEGER" and types["month"] == "DATE"
    require_validation(verify_release(tmp_path / "master"))
    names = verify_release(tmp_path / "master")["inputs"]
    assert sum(k.startswith("capiq_est_") for k in names) == 8 and sum(k.startswith("capiq_fund_") for k in names) == 4      # every Capital IQ file the release used is hashed in its manifest
    assert rep["gates"]["quarterly_family_replicates_engine"] == "pass" and all(rep["gates"][f"{f}_replicates_engine"] == "pass" for f in pf.FAMILIES)
    q2 = "select count(numest_fy1), count(days_since_ann), count(sales_surprise_sym), count(io_frac), count(esg_rating_chg) from read_parquet('{}') where month = DATE '{}'"
    assert con.execute(q2.format(p.as_posix(), "2026-07-01")).fetchone() == (52, 59, 59, 60, 59)    # estimates: 26 I/B/E/S (even stocks) + 26 Capital IQ (odd); the 120-day families are present, 103 days on
    assert con.execute(q2.format(p.as_posix(), "2026-10-01")).fetchone() == (52, 0, 0, 0, 59)       # October: Capital IQ only for estimates; 195 days: only ESG (548-day window) remains
    q3 = "select count(*) filter (where ibes_cons_ciq), count(*) filter (where numest_fy1 >= 100), count(*) filter (where numest_fy1 < 100), count(*) filter (where ibes_cons_ciq and kd_ibes_cons is not null), count(rev_fy1_3m) from read_parquet('{}') where month = DATE '{}'"
    assert con.execute(q3.format(p.as_posix(), "2026-07-01")).fetchone() == (26, 26, 26, 0, 52)      # I/B/E/S values are never overwritten (>= 100 kept), Capital IQ fills only where there is no I/B/E/S record; 3m revision exists from the April file
    assert con.execute(q3.format(p.as_posix(), "2026-10-01")).fetchone() == (52, 0, 52, 0, 52)
    assert con.execute("select count(*) filter (where ciq_est_same_day) from read_parquet('{}') where month = DATE '2026-10-01'".format(p.as_posix())).fetchone()[0] == 6   # estimates dated on the as-of day are flagged
    q = con.execute(f"select count(*), count(gp_at_chg), count(sue_ts), count(earn_chg_me), min(kd_fund), max(days_since_rdq) from read_parquet('{p.as_posix()}') where month = DATE '2026-10-01'").fetchone()
    assert q[:4] == (60, 60, 59, 60) and str(q[4])[:10] == "2026-03-15" and q[5] == 201.0       # sue_ts missing for the one stock; Oct 1 is 201 days after the 14 March filing


def test_assemble_fills_gaps_when_the_engine_has_the_month(tmp_path):
    build_base(tmp_path / "base", n=120, last_engine="2026-07-01")                      # 120 stocks: enough with an estimate for the parity checks (at least 30 stocks) to run
    build_ciq(tmp_path / "ciq", n=120)
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "master", pd.Timestamp("2026-07-01"))
    assert all(v == "pass" for v in rep["gates"].values()), rep["gates"]
    assert rep["months"]["july"]["kind"] == "engine_fill" and rep["months"]["aug"]["kind"] == "bridge"
    p = (tmp_path / "master" / "master_panel.parquet").as_posix()
    con = duckdb.connect()
    jul = con.execute(f"select count(*) filter (where bm is null), count(*) filter (where bm_ciq) from read_parquet('{p}') where month = DATE '2026-07-01' and not bridge").fetchone()
    assert jul == (0, 12)                                             # the twelve engine gaps (every tenth stock) were filled and flagged


def test_assemble_refuses_a_wrong_format_file_and_fails_gates_on_bad_matching_or_a_shrunken_universe(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")
    make_file(tmp_path / "ciq/capiq_fund_aug_2026.xlsx", pricing="07/31/2026", asof="08/01/2026", period="Latest Fiscal Year")
    with pytest.raises(CiqFormatError):
        run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m1", pd.Timestamp("2026-06-01"))
    build_ciq(tmp_path / "ciq2")
    broken = company_rows(60, seed=10)
    for r in broken[:30]:
        r[2], r[3] = "ZZZZZZZZ0", "ZZ"                             # half the companies cannot be matched
    make_file(tmp_path / "ciq2/capiq_fund_sep_2026.xlsx", broken, pricing="08/31/2026", asof="09/01/2026")
    rep = run(tmp_path / "base", tmp_path / "ciq2", tmp_path / "m2", pd.Timestamp("2026-06-01"))
    assert rep["gates"]["match_sep"] == "fail" and rep["gates"]["universe_size_sep"] == "fail"             # only half the universe survives into the September bridge
    assert rep["gates"]["match_aug"] == "pass"


def test_assemble_fails_the_estimate_gates_on_bad_matching_and_refuses_a_missing_file(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")
    broken = est_rows(60, 5, "2026-09-01")
    for r in broken[:30]:
        r[2], r[3] = "ZZZZZZZZ0", "ZZ"                                             # half the companies cannot be matched
    make_est_file(tmp_path / "ciq/capiq_est_sep_2026.xlsx", broken, asof="09/01/2026")
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m1", pd.Timestamp("2026-06-01"))
    assert rep["gates"]["est_match_sep"] == "fail" and rep["gates"]["est_match_aug"] == "pass"
    (tmp_path / "ciq/capiq_est_apr_2026.xlsx").unlink()
    with pytest.raises(FileNotFoundError):
        run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m2", pd.Timestamp("2026-06-01"))                    # a missing estimates file is an error, never a silent skip
    rep2 = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m3", pd.Timestamp("2026-06-01"), estimates=False)
    assert "est_parity_vs_engine" not in rep2["gates"]


def test_a_feature_that_fails_its_parity_check_is_not_filled_while_the_others_are(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")
    jun = est_rows(60, 3, "2026-06-01")                                           # scramble the June means: the engine (built from the original files) and Capital IQ now disagree on dispersion and revisions
    for i, r in enumerate(jun):
        r[4:9] = est_rows(60, 3, "2026-06-01")[59 - i][4:9]
    make_est_file(tmp_path / "ciq/capiq_est_jun_2026.xlsx", jun, asof="06/01/2026")
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m", pd.Timestamp("2026-06-01"))
    assert rep["gates"]["est_parity_numest_fy1"] == "pass" and rep["gates"]["est_parity_rev_fy1_1m"] == "fail"
    assert "numest_fy1" in rep["estimates_filled_features"] and "rev_fy1_1m" not in rep["estimates_filled_features"]
    p = (tmp_path / "m" / "master_panel.parquet").as_posix()
    n, r1 = duckdb.connect().execute(f"select count(numest_fy1), count(rev_fy1_1m) from read_parquet('{p}') where month = DATE '2026-10-01'").fetchone()
    assert n == 52 and r1 == 0                                                    # October: counts filled from Capital IQ, the failed revision feature left empty


def test_assemble_fills_price_features_per_feature_and_never_overwrites(tmp_path):
    build_base(tmp_path / "base", yahoo_dir=tmp_path / "yahoo")
    build_ciq(tmp_path / "ciq")
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m", pd.Timestamp("2026-06-01"), yahoo_dir=tmp_path / "yahoo")
    g = rep["gates"]
    assert g["px_parity_ret_1m"] == "pass" and g["px_parity_vol_1m"] == "pass" and g["px_parity_turnover_1m"] == "pass"
    assert g["px_parity_ret_36_13"] == "fail" and g["px_parity_abn_turnover"] == "fail"                  # the fixture has no history for these: untested features are not filled
    assert {"ret_1m", "vol_1m", "turnover_1m"} <= set(rep["px_filled_features"]) and not {"abn_turnover", "ret_36_13", "log_me"} & set(rep["px_filled_features"])
    p = (tmp_path / "m" / "master_panel.parquet").as_posix()
    con = duckdb.connect()
    n, na, flag = con.execute(f"select count(ret_1m), count(abn_turnover), count(*) filter (where px_yahoo) from read_parquet('{p}') where month = DATE '2026-10-01'").fetchone()
    assert n == 60 and na == 0 and flag == 60
    bad = con.execute(f"select count(*) from read_parquet('{p}') where bridge and abs(log_me - ln(me_lag)) > 1e-9").fetchone()[0]
    assert bad == 0                                                                                       # log_me is never overwritten
    assert "yahoo_files_digest" in verify_release(tmp_path / "m")["inputs"]
    assert g["column_types_match_base"] == "pass"
    ty = {r[0]: r[1] for r in con.execute(f"describe select * from read_parquet('{p}')").fetchall()}
    assert ty["kd_ear_rdq"] == "DATE" and ty["n_8k_90d"] in ("BIGINT", "DOUBLE") and ty["month"] == "DATE"                 # dates stay dates in the output
    assert g["ev_parity_ear_rdq"] == "pass" and g["ev_parity_ear_ann"] == "pass" and g["ev_parity_abn_vol_ann"] == "pass" and g["ev_parity_post_ann_car"] == "pass"
    assert set(rep["ev_filled_features"]) == {"ear_ann", "abn_vol_ann", "post_ann_car", "ear_rdq"}
    n1, n2, n3, flag, kd = con.execute(f"select count(ear_rdq), count(abn_vol_ann), count(post_ann_car), count(*) filter (where ann_events_proxy), count(kd_ear_rdq) from read_parquet('{p}') where month = DATE '2026-10-01'").fetchone()
    assert (n1, n2, n3, flag, kd) == (60, 60, 60, 60, 60)                                 # every stock reported on 12 to 16 August, 46 to 50 days before the October decision


def test_sec_event_counts_in_bridge_months_follow_the_windows_and_the_coverage_end(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m", pd.Timestamp("2026-06-01"))
    assert rep["gates"]["edgar_replicates_engine"] == "pass"
    p = (tmp_path / "m" / "master_panel.parquet").as_posix()
    q = "select count(n_8k_90d), min(n_8k_90d), max(n_8k_90d), min(abn_8k), max(abn_8k), sum(n_sc13d_365d), sum(n_nt_365d), sum(n_8k_502_365d) from read_parquet('{}') where month = DATE '{}'"
    con = duckdb.connect()
    assert con.execute(q.format(p, "2026-07-01")).fetchone() == (58, 1, 1, 0.75, 0.75, 20, 58, 58)        # 58 covered stocks (no CIK link, not on the EDGAR list); 8-K of 10 May in the 90-day window, 20 Feb one in the base
    assert con.execute(q.format(p, "2026-09-01")).fetchone() == (58, 0, 0, -0.5, -0.5, 20, 58, 58)         # 10 May is now 114 days old: two base 8-Ks, none in the window; the 15 Sep event is after the decision date
    assert con.execute(q.format(p, "2026-10-01")).fetchone() == (0, None, None, None, None, 20, None, None)     # the table ends 15 Sep: an October window is not covered, so the counts are empty rather than short,
    # except n_sc13d_365d, computed on the window 16 days short (within the 30-day truncation BRIDGE S3 tested) and flagged
    assert con.execute(f"select bool_and(sc13d_truncated), count(*) from read_parquet('{p}') where month = DATE '2026-10-01'").fetchone() == (True, 60)
    assert not con.execute(f"select bool_or(sc13d_truncated) from read_parquet('{p}') where month = DATE '2026-09-01'").fetchone()[0]


def test_release_seals_delisting_and_incomplete_flags_even_on_an_unsealed_panel(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "master", pd.Timestamp("2026-06-01"))
    assert rep["gates"]["forward_returns_sealed"] == "pass"
    p = (tmp_path / "master" / "master_panel.parquet").as_posix()
    con = duckdb.connect()
    cols = {r[0] for r in con.execute(f"describe select * from read_parquet('{p}')").fetchall()}
    inc = [c for c in cols if c.startswith("fwd_incomplete_")]
    assert inc                                                                                  # the fixture carries the flags, False in the engine months before sealing
    for c in inc:
        assert con.execute(f"select count(*) from read_parquet('{p}') where month >= DATE '2026-01-01' and {c} is distinct from true").fetchone()[0] == 0
    for c in (c for c in cols if c.startswith("fwd_delist_") and not c.startswith("fwd_delist_ret")):
        assert con.execute(f"select count({c}) from read_parquet('{p}') where month >= DATE '2026-01-01'").fetchone()[0] == 0


def test_live_sec_counts_fill_only_empty_cells_behind_the_gate(tmp_path):
    from quant.features.ciqfill.assemble import fill_sec_counts_live
    n = 100
    rng = np.random.default_rng(3)
    base = rng.integers(0, 6, n).astype(float)
    sep = pd.DataFrame({"permno": np.arange(n), "month": pd.Timestamp("2026-09-01"), "in_investable": True, "n_8k_502_365d": base, "n_nt_365d": base})
    octo = pd.DataFrame({"permno": np.arange(n), "month": pd.Timestamp("2026-10-01"), "in_investable": True, "n_8k_502_365d": np.nan, "n_nt_365d": np.nan})
    octo.loc[0, "n_8k_502_365d"] = 99.0                                                         # an engine value is never overwritten
    live = pd.DataFrame({"n_8k_502_365d": base + (rng.random(n) < 0.05), "n_nt_365d": rng.permutation(base)}, index=pd.Index(np.arange(n), name="permno"))
    f = tmp_path / "events_asof.parquet"
    live.to_parquet(f)
    gates, rep = {}, {}
    fill_sec_counts_live(f, pd.Timestamp("2026-10-01"), [sep, octo], gates, rep)
    assert gates == {"sec_live_n_8k_502_365d": "pass", "sec_live_n_nt_365d": "fail"}             # a shuffled column does not track the previous month: not filled
    assert octo.loc[0, "n_8k_502_365d"] == 99.0 and octo["n_8k_502_365d"].iloc[1:].tolist() == live["n_8k_502_365d"].iloc[1:].tolist()
    assert octo["n_nt_365d"].isna().all() and bool(octo["sec_counts_live"].all())


def test_a_later_decision_month_is_built_when_its_files_are_present(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq", through=11)
    rep = run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m", pd.Timestamp("2026-06-01"), through="2026-11-01")
    assert list(rep["months"]) == ["july", "aug", "sep", "oct", "nov"]
    assert (rep["months"]["nov"]["kind"], rep["months"]["nov"]["decision_month"]) == ("bridge", "2026-11-01") and rep["gates"]["match_nov"] == "pass" and rep["gates"]["est_match_nov"] == "pass"
    p = (tmp_path / "m" / "master_panel.parquet").as_posix()
    assert duckdb.connect().execute(f"select count(*) filter (where bridge) from read_parquet('{p}') where month = DATE '2026-11-01'").fetchone()[0] == 60


def test_a_decision_month_up_to_through_without_its_files_is_an_error_not_a_skip(tmp_path):
    build_base(tmp_path / "base")
    build_ciq(tmp_path / "ciq")                                                    # files to October only
    with pytest.raises(FileNotFoundError, match="capiq_est_nov_2026"):
        run(tmp_path / "base", tmp_path / "ciq", tmp_path / "m", pd.Timestamp("2026-06-01"), through="2026-11-01")


def test_seal_leaks_counts_outcomes_after_their_seal_and_unset_incomplete_flags_for_the_columns_present():
    from quant.features.ciqfill.assemble import seal_leaks

    months = pd.to_datetime(["2025-10-01", "2025-12-01", "2026-01-01", "2026-02-01"]).date
    d = pd.DataFrame({"month": months, "fwd_ret_1m": [0.1, 0.2, 0.3, np.nan], "fwd_ret_3m": [0.1, np.nan, np.nan, np.nan],
                      "fwd_incomplete_1m": [False, False, True, None], "fwd_incomplete_3m": [False, True, True, True]})
    con = duckdb.connect()
    con.register("d", d)
    values, flags = seal_leaks(con, "d", set(d.columns))
    assert values == {"fwd_ret_1m": 1, "fwd_ret_3m": 0}                         # 1m is sealed from 2026-01 (one value there), 3m from 2025-11
    assert flags == {"fwd_incomplete_1m": 1, "fwd_incomplete_3m": 0}            # a missing flag after the seal is not TRUE
    assert seal_leaks(con, "d", {"month", "fwd_ret_3m"}) == ({"fwd_ret_3m": 0}, {})


def test_month_keys_keep_the_2026_gate_names_and_add_the_year_after_2026(tmp_path):
    from quant.features.ciqfill import assemble as A
    assert A.month_key(pd.Timestamp("2026-11-01")) == "nov" and A.month_key(pd.Timestamp("2026-07-01"), fund=True) == "july"
    assert A.month_key(pd.Timestamp("2027-01-01")) == "jan_2027" and A.month_key(pd.Timestamp("2027-07-01"), fund=True) == "july_2027"
    assert A.fund_path(tmp_path, pd.Timestamp("2027-01-01")).name == "capiq_fund_jan_2027.xlsx"
    assert A.est_path(tmp_path, pd.Timestamp("2027-02-01")).name == "capiq_est_feb_2027.xlsx"
    assert [m.strftime("%Y-%m") for m in A.fund_files(pd.Timestamp("2027-02-01"))] == ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02"]


def test_estimates_load_across_the_year_end_and_refuse_a_wrongly_dated_2027_file(tmp_path):
    from quant.features.ciqfill import assemble as A
    months = pd.date_range("2026-04-01", "2027-01-01", freq="MS")
    for i, m in enumerate(months):
        make_est_file(A.est_path(tmp_path, m), est_rows(20, i, m.date().isoformat()), asof=m.strftime("%m/%d/%Y"))
    est = A.load_estimates(tmp_path, pd.Timestamp("2027-01-01"))
    assert set(est) == {A.month_key(m) for m in months} and est["jan_2027"].asof == pd.Timestamp("2027-01-01")
    make_est_file(A.est_path(tmp_path, pd.Timestamp("2027-01-01")), est_rows(20, 9, "2026-12-01"), asof="12/01/2026")
    with pytest.raises(SystemExit, match="capiq_est_jan_2027.xlsx is as of 2026-12-01"):
        A.load_estimates(tmp_path, pd.Timestamp("2027-01-01"))
