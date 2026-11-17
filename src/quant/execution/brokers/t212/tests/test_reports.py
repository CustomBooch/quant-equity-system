"""quant.execution.brokers.t212.reports: report files, written once. Real files in tmp_path."""
from __future__ import annotations

import datetime as dt

import pytest

from quant.execution.brokers.t212 import reports as R


def test_a_report_is_a_json_file_and_its_markdown_summary_under_one_stem(tmp_path):
    j, m = R.write("reconcile_20261027T150000Z", {"b": 1, "a": "é"}, "# Report", tmp_path / "reports")
    assert j.read_text(encoding="utf-8") == '{\n "a": "é",\n "b": 1\n}\n' and m.read_text(encoding="utf-8") == "# Report\n"
    assert (j.name, m.name) == ("reconcile_20261027T150000Z.json", "reconcile_20261027T150000Z.md")


def test_the_same_payload_gives_the_same_bytes_whatever_its_key_order():
    assert R.dumps({"x": [1, 2], "a": {"z": 1, "b": 2}}) == R.dumps({"a": {"b": 2, "z": 1}, "x": [1, 2]})


def test_an_existing_report_is_never_overwritten_and_nothing_is_written(tmp_path):
    (tmp_path / "r.md").write_text("kept")
    with pytest.raises(FileExistsError, match="never overwritten"):
        R.write("r", {"a": 1}, "new", tmp_path)
    assert not (tmp_path / "r.json").exists() and (tmp_path / "r.md").read_text() == "kept"


def test_a_stamp_is_the_utc_time_and_a_naive_time_is_refused():
    assert R.stamp(dt.datetime(2026, 10, 27, 17, 5, 9, tzinfo=dt.timezone(dt.timedelta(hours=2)))) == "20261027T150509Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        R.stamp(dt.datetime(2026, 10, 27, 15, 5, 9))
