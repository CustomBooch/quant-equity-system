"""Fixtures shared by the portfolio and execution tests."""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest


@pytest.fixture()
def yahoo(monkeypatch):
    """yfinance.download answering with fixed daily closes (two days; the newest has a missing close for one ticker)."""
    def download(tickers, **kw):
        idx = pd.to_datetime(["2026-10-22", "2026-10-23"])
        close = pd.DataFrame({"AAA": [49.0, 50.0], "BBB": [20.0, float("nan")], "DEAD": [0.0, 0.0]}, index=idx)[list(tickers)]
        return pd.concat({"Close": close}, axis=1)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
