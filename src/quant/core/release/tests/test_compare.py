"""quant.core.release.compare: which columns differ between two panels, whatever their row order."""
from __future__ import annotations

import pandas as pd


def test_compare_finds_changed_and_one_sided_columns_regardless_of_row_order(tmp_path):
    from quant.core.release import compare as CM

    a = pd.DataFrame({"permno": [1, 2, 3], "month": pd.to_datetime(["2026-01-01"] * 3), "x": [1.0, 2.0, 3.0], "y": [1, 1, 1], "gone": [0, 0, 0]})
    b = a.drop(columns="gone").assign(new=[5, 5, 5]).iloc[::-1]                 # same x, reversed rows
    b.loc[b["permno"] == 2, "y"] = 9
    a.to_parquet(tmp_path / "a.parquet")
    b.to_parquet(tmp_path / "b.parquet")
    r = CM.compare(tmp_path / "a.parquet", tmp_path / "b.parquet")
    assert r["changed"] == ["y"] and r["only_old"] == ["gone"] and r["only_new"] == ["new"] and r["rows"] == {"old": 3, "new": 3}
    assert CM.compare(tmp_path / "a.parquet", tmp_path / "b.parquet", where="permno <> 2")["changed"] == []


def test_a_column_name_from_the_file_is_quoted_and_never_runs_as_sql(tmp_path):
    from quant.core.release import compare as CM

    evil = 'x)) FROM range(1); COPY (SELECT 1) TO \'pwned.csv\'; SELECT sum(hash(1'
    a = pd.DataFrame({"permno": [1, 2], "month": pd.to_datetime(["2026-01-01"] * 2), evil: [1.0, 2.0], 'quo"te': [1, 2]})
    a.to_parquet(tmp_path / "a.parquet")
    a.assign(**{'quo"te': [1, 3]}).to_parquet(tmp_path / "b.parquet")
    r = CM.compare(tmp_path / "a.parquet", tmp_path / "b.parquet")
    assert r["changed"] == ['quo"te'] and not (tmp_path / "pwned.csv").exists() and not list(tmp_path.glob("**/pwned.csv"))
