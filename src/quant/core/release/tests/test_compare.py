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
