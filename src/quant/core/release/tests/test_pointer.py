"""quant.core.release.pointer: promoting a release moves the current one to previous, keeps only matching waivers, and refuses a tampered or repeated release."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from quant.core.release import pointer as PT
from quant.core.release import write_release


def make_release(d, gates, first_permno=1):
    d.mkdir()
    pd.DataFrame({"permno": [first_permno, 2], "month": pd.to_datetime(["2026-09-01", "2026-10-01"])}).to_parquet(d / "master_panel.parquet")
    (d / "report.json").write_text("{}")
    write_release(d, d.name, validation={"report_sha256": PT.sha256_file(d / "report.json"), "gates": gates}, code_commit="abc")


def test_promote_moves_current_to_previous_and_keeps_only_matching_waivers(tmp_path):
    make_release(tmp_path / "r1", {"a": "pass", "b": "fail"})
    make_release(tmp_path / "r2", {"a": "pass", "c": "fail"}, first_permno=3)
    ptr = tmp_path / "MASTER_CURRENT.json"
    old = PT.entry_for(tmp_path / "r1", "first", [{"gates": ["b"], "by": "owner"}])
    ptr.write_text(json.dumps({"current": old}))
    new = PT.promote(tmp_path / "r2", "second", "rollback only", pointer=ptr)
    doc = json.loads(ptr.read_text())
    assert doc["current"] == new and doc["previous_release"]["sha256"] == old["sha256"] and doc["previous_release"]["note"] == "rollback only"
    assert new["rows"] == 2 and new["columns"] == 2 and new["months"] == "2026-09 to 2026-10" and new["gates"] == {"total": 2, "pass": 1, "fail": ["c"]}
    assert new["owner_waivers"] == []                                         # the waiver for gate b does not carry to a release without gate b
    with pytest.raises(SystemExit, match="already current"):
        PT.promote(tmp_path / "r2", "again", "x", pointer=ptr)


def test_the_promotion_time_is_the_injected_clock(tmp_path):
    import datetime as dt

    make_release(tmp_path / "r1", {"a": "pass"})
    make_release(tmp_path / "r2", {"a": "pass"}, first_permno=3)
    ptr = tmp_path / "MASTER_CURRENT.json"
    t0, t1 = (dt.datetime(2026, 10, d, 9, 0, tzinfo=dt.timezone.utc) for d in (26, 27))
    ptr.write_text(json.dumps({"current": PT.entry_for(tmp_path / "r1", "first", [], now=t0)}))
    new = PT.promote(tmp_path / "r2", "second", "rollback only", pointer=ptr, now=t1)
    assert new["promoted_utc"] == "2026-10-27T09:00:00+00:00" and PT.read(ptr)["previous_release"]["promoted_utc"] == "2026-10-26T09:00:00+00:00"


def test_promote_refuses_a_tampered_release(tmp_path):
    make_release(tmp_path / "r1", {"a": "pass"})
    (tmp_path / "r1" / "report.json").write_text('{"changed": 1}')
    with pytest.raises(Exception, match="differs"):
        PT.entry_for(tmp_path / "r1", "x", [])


def test_the_commands_show_the_current_release_and_promote_with_this_repositorys_commit(tmp_path, capsys):
    make_release(tmp_path / "r1", {"a": "pass"})
    make_release(tmp_path / "r2", {"a": "pass"}, first_permno=3)
    ptr = tmp_path / "MASTER_CURRENT.json"
    ptr.write_text(json.dumps({"current": PT.entry_for(tmp_path / "r1", "first", [])}))
    PT.main(["show"], pointer=ptr)
    assert json.loads(capsys.readouterr().out)["sha256"] == PT.read(ptr)["current"]["sha256"]
    PT.main(["promote", str(tmp_path / "r2"), "--note", "second", "--previous-note", "rollback only"], pointer=ptr)
    doc = PT.read(ptr)
    assert doc["current"]["note"] == "second" and doc["current"]["code_commit"] == PT.head_commit() and doc["previous_release"]["note"] == "rollback only"


def test_promotion_without_both_notes_is_refused_before_anything_is_written(tmp_path):
    make_release(tmp_path / "r1", {"a": "pass"})
    ptr = tmp_path / "MASTER_CURRENT.json"
    ptr.write_text(json.dumps({"current": PT.entry_for(tmp_path / "r1", "first", [])}))
    before = ptr.read_bytes()
    with pytest.raises(SystemExit):
        PT.main(["promote", str(tmp_path / "r1"), "--note", "only one note"], pointer=ptr)
    assert ptr.read_bytes() == before


def test_a_pointer_saved_by_an_editor_with_a_byte_order_mark_is_read(tmp_path):
    make_release(tmp_path / "r1", {"a": "pass"})
    ptr = tmp_path / "MASTER_CURRENT.json"
    entry = PT.entry_for(tmp_path / "r1", "first", [])
    ptr.write_bytes(json.dumps({"current": entry}).encode("utf-8-sig"))
    assert PT.read(ptr)["current"]["sha256"] == entry["sha256"]


def test_a_release_folder_whose_name_holds_a_quote_and_sql_is_read_as_a_path_not_run(tmp_path):
    name = "r'); COPY (SELECT 1) TO 'pwned.csv'; SELECT ('"
    make_release(tmp_path / name, {"a": "pass"})
    entry = PT.entry_for(tmp_path / name, "x", [])
    assert entry["rows"] == 2 and not list(tmp_path.glob("**/pwned.csv")) and not (tmp_path / "pwned.csv").exists()
