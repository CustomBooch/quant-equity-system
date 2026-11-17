"""quant.execution.brokers.t212.pull_instruments: one read-only GET, the body stored as received where quant.data.instruments.t212 looks for the newest list. Boundaries replaced: urlopen and the repository root."""
from __future__ import annotations

import datetime as dt
import io
import json
import urllib.request

from quant.execution.brokers.t212 import api
from quant.data.instruments import t212 as M
from quant.execution.brokers.t212 import pull_instruments as P

LIST = [{"ticker": "A_US_EQ", "isin": "US0000000001", "shortName": "A", "currencyCode": "USD", "type": "STOCK"}]


def test_the_list_is_stored_unchanged_under_todays_date_and_becomes_the_newest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("T212_READ_API_KEY", "k")
    monkeypatch.setenv("T212_READ_SECRET_KEY", "s")
    monkeypatch.setattr(M, "REPO", tmp_path)
    old = M.instruments_file("2026-01-02")
    old.parent.mkdir(parents=True)
    old.write_text("[]")
    body = json.dumps(LIST, indent=3).encode()
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: seen.append((req.get_method(), req.full_url)) or io.BytesIO(body))
    P.main()
    f = M.instruments_file(dt.date.today().isoformat())
    assert seen == [("GET", api.DEMO + "/equity/metadata/instruments")] and f.read_bytes() == body
    assert M.latest_instruments_file() == f and M.latest_instruments().to_dict("records") == LIST
