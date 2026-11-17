"""quant.ops.borrow_watch: which held shorts are no longer easy to borrow. The ledger, the asset list and the target books are files in tmp_path; the clock is
a parameter. Nothing here reaches a broker, and the module has no way to."""
from __future__ import annotations

import datetime as dt
import inspect
import json

import pandas as pd
import pytest

from quant.data.instruments import alpaca as AL
from quant.ops import borrow_watch as BW

TODAY = dt.date(2026, 11, 17)


def assets(**flags):
    """An asset list of four names; `flags` overrides one symbol's fields, e.g. BBB={'easy_to_borrow': False}."""
    rows = {s: {"symbol": s, "status": "active", "tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB", "CCC", "LONG")}
    for s, f in flags.items():
        rows[s].update(f)
    return pd.DataFrame(list(rows.values()))


LEDGER = {"0": {"AAA": -10.0, "BBB": -5.0, "LONG": 20.0}, "1": {"AAA": -4.0, "CCC": -3.0}}


class TestHeldShorts:
    def test_a_short_is_a_negative_total_across_tranches(self):
        assert BW.held_shorts(LEDGER) == {"AAA": -14.0, "BBB": -5.0, "CCC": -3.0}

    def test_a_name_short_in_one_tranche_and_long_in_another_nets_out(self):
        assert BW.held_shorts({"0": {"AAA": -10.0}, "1": {"AAA": 10.0}}) == {}

    def test_an_empty_ledger_holds_no_shorts(self):
        assert BW.held_shorts({}) == {}


class TestCheck:
    def test_a_name_that_is_still_easy_to_borrow_has_no_reason(self):
        assert BW.check({"AAA": -1.0}, assets()) == {"AAA": []}

    @pytest.mark.parametrize("field,value,reason", [("easy_to_borrow", False, "not easy to borrow"), ("shortable", False, "not shortable"),
                                                      ("tradable", False, "not tradable"), ("status", "inactive", "not active")])
    def test_each_flag_that_fails_is_named(self, field, value, reason):
        assert BW.check({"AAA": -1.0}, assets(AAA={field: value})) == {"AAA": [reason]}

    def test_several_failed_flags_are_all_named(self):
        got = BW.check({"AAA": -1.0}, assets(AAA={"easy_to_borrow": False, "shortable": False}))
        assert got == {"AAA": ["not shortable", "not easy to borrow"]}

    def test_a_symbol_missing_from_the_list_is_reported_as_such(self):
        assert BW.check({"ZZZ": -1.0}, assets()) == {"ZZZ": ["not in the asset list"]}


class TestReport:
    def test_a_clean_book_on_a_fresh_list_has_no_findings(self):
        out = BW.report(LEDGER, assets(), "2026-11-16", TODAY, {"AAA": -0.026, "BBB": -0.026, "CCC": -0.026})
        assert out["findings"] == [] and out["short_names"] == 3 and out["largest_short_target_weight"] == pytest.approx(0.026)

    def test_a_held_short_that_turned_hard_to_borrow_is_a_finding(self):
        out = BW.report(LEDGER, assets(BBB={"easy_to_borrow": False}), "2026-11-16", TODAY)
        assert out["findings"] == ["BBB: not easy to borrow"]

    def test_a_list_older_than_three_days_is_a_finding_first(self):
        out = BW.report(LEDGER, assets(), "2026-11-13", TODAY)
        assert out["findings"] == ["asset list is 4 days old; refresh it before relying on this check"]
        assert BW.report(LEDGER, assets(), "2026-11-14", TODAY)["findings"] == []

    def test_a_missing_list_date_is_a_finding(self):
        assert BW.report(LEDGER, assets(), None, TODAY)["findings"][0].startswith("asset list is missing")

    def test_a_name_without_a_target_weight_still_reports_its_shares(self):
        out = BW.report(LEDGER, assets(), "2026-11-16", TODAY, {"AAA": -0.03})
        assert [r["target_weight"] for r in out["shorts"]] == [-0.03, None, None] and out["largest_short_target_weight"] == pytest.approx(0.03)

    def test_with_no_shorts_there_is_nothing_to_find_but_the_list_still_has_to_be_fresh(self):
        assert BW.report({}, assets(), "2026-11-16", TODAY)["findings"] == []
        assert BW.report({}, assets(), "2026-10-01", TODAY)["findings"] != []


class TestTargetWeights:
    def test_the_newest_alpaca_book_gives_the_weights(self, tmp_path):
        pd.DataFrame({"ticker": ["OLD"], "w": [-0.01]}).to_csv(tmp_path / "target_book_X_master_2026-09_alpaca.csv", index=False)
        pd.DataFrame({"ticker": ["AAA", "BBB"], "w": [-0.026, 0.05]}).to_csv(tmp_path / "target_book_X_master_2026-10_alpaca.csv", index=False)
        pd.DataFrame({"ticker": ["A11"], "w": [-0.5]}).to_csv(tmp_path / "target_book_X_master_2026-10_alpaca_a11.csv", index=False)
        assert BW.target_weights(tmp_path) == {"AAA": -0.026, "BBB": 0.05}

    def test_no_book_gives_no_weights(self, tmp_path):
        assert BW.target_weights(tmp_path) == {}


class TestCommand:
    @pytest.fixture()
    def files(self, tmp_path, monkeypatch):
        """An asset list pulled 2026-11-16, a ledger and a target book, all under tmp_path."""
        monkeypatch.setattr(AL, "assets_file", lambda day="*": tmp_path / f"alpaca_{day}" / "assets.json")
        folder = tmp_path / "alpaca_2026-11-16"
        folder.mkdir()
        (folder / "assets.json").write_text(assets(BBB={"easy_to_borrow": False}).to_json(orient="records"))
        (tmp_path / "ledger.json").write_text(json.dumps(LEDGER))
        pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "w": [-0.026, -0.026, -0.026]}).to_csv(tmp_path / "target_book_X_alpaca.csv", index=False)
        return tmp_path

    def test_a_hard_to_borrow_short_exits_1_and_is_named(self, files, capsys):
        assert BW.main([], today=TODAY, ledger_path=files / "ledger.json", books=files) == 1
        out = capsys.readouterr().out
        assert "FINDING BBB" in out and "OK      AAA" in out and "borrow-watch: 1 finding(s)" in out and "target -2.60%" in out

    def test_the_json_output_carries_the_findings(self, files, capsys):
        assert BW.main(["--json"], today=TODAY, ledger_path=files / "ledger.json", books=files) == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc["findings"] == ["BBB: not easy to borrow"] and doc["list_date"] == "2026-11-16" and doc["short_names"] == 3

    def test_with_no_shorts_held_the_command_exits_0(self, files, capsys):
        (files / "ledger.json").write_text("{}")
        assert BW.main([], today=TODAY, ledger_path=files / "ledger.json", books=files) == 0
        assert "0 held short(s)" in capsys.readouterr().out

    def test_without_an_asset_list_it_refuses_with_exit_2_and_the_command_to_run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(AL, "assets_file", lambda day="*": tmp_path / f"alpaca_{day}" / "assets.json")
        assert BW.main([], today=TODAY, ledger_path=tmp_path / "none.json", books=tmp_path) == 2
        assert "REFUSED: no Alpaca asset list" in capsys.readouterr().out


def test_the_module_cannot_reach_a_broker():
    src = inspect.getsource(BW)
    assert "urllib" not in src and "requests" not in src and "alpaca.api" not in src and "http" not in src.replace("https://", "").lower().split('"""')[2]
