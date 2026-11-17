"""quant.data.instruments.alpaca: CRSP tickers to Alpaca symbols. Local data only (asset lists built here; CRSP ids passed in)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from quant.data.instruments import alpaca as M


def assets(rows):
    return pd.DataFrame([{"symbol": s, "name": s, "status": st, "tradable": tr, "fractionable": True, "shortable": True, "easy_to_borrow": True, "exchange": "NYSE"}
                         for s, st, tr in rows])


class TestMapIds:
    def test_a_tradable_ticker_maps_to_itself(self):
        out = M.map_ids(pd.DataFrame({"cusip": ["1"], "ticker": ["AAPL"]}), assets([("AAPL", "active", True)]))
        assert out["line"].tolist() == ["AAPL"]

    def test_a_crsp_ticker_without_its_share_class_maps_to_the_one_dotted_symbol(self):
        out = M.map_ids(pd.DataFrame({"cusip": ["1"], "ticker": ["BRK"]}), assets([("BRK.B", "active", True), ("BRKX", "active", True)]))
        assert out["line"].tolist() == ["BRK.B"]

    def test_two_dotted_candidates_are_ambiguous_and_give_no_line(self):
        out = M.map_ids(pd.DataFrame({"cusip": ["1"], "ticker": ["XYZ"]}), assets([("XYZ.A", "active", True), ("XYZ.B", "active", True)]))
        assert out["line"].tolist() == [None]

    def test_inactive_untradable_and_missing_tickers_give_no_line(self):
        a = assets([("OLD", "inactive", True), ("HALT", "active", False)])
        out = M.map_ids(pd.DataFrame({"cusip": ["1", "2", "3"], "ticker": ["OLD", "HALT", None]}), a)
        assert out["line"].tolist() == [None, None, None]


class TestFiles:
    def test_the_newest_pull_is_read_and_its_date_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(M, "REPO", tmp_path)
        for day, sym in (("2026-11-01", "OLD"), ("2026-11-12", "NEW")):
            f = M.assets_file(day)
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps([{"symbol": sym, "status": "active", "tradable": True}]))
        assert M.list_date() == "2026-11-12" and M.latest_assets()["symbol"].tolist() == ["NEW"]

    def test_no_pull_refuses_with_the_command_to_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(M, "REPO", tmp_path)
        assert M.list_date() is None
        with pytest.raises(FileNotFoundError, match="pull_assets"):
            M.latest_assets()
