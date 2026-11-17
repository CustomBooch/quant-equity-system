"""quant.execution.brokers.alpaca.pull_assets: the read-only asset pull. HTTP replaced at urlopen; files go to tmp_path."""
from __future__ import annotations

import datetime as dt
import io
import json
import urllib.request

from quant.data.instruments import alpaca as M
from quant.execution.brokers.alpaca import pull_assets as PA


def test_the_pull_is_one_get_and_stores_only_the_listed_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "REPO", tmp_path)
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "kid")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "sec")
    seen = []

    def urlopen(req, timeout):
        seen.append((req.get_method(), req.full_url))
        return io.BytesIO(json.dumps([{"symbol": "AAPL", "status": "active", "tradable": True, "id": "secret-internal-id"}]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    f = PA.pull(dt.date(2026, 11, 12))
    assert seen == [("GET", "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity")]
    rows = json.loads(f.read_text())
    assert f == M.assets_file("2026-11-12") and set(rows[0]) == set(M.COLUMNS) and rows[0]["symbol"] == "AAPL"
