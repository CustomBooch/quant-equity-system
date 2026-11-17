"""quant.execution.brokers.alpaca.api: read-only access to the Alpaca paper account. HTTP is the boundary: urlopen is replaced, nothing reaches the network."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from quant.execution.brokers.alpaca import api


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "kid")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "sec")


@pytest.fixture()
def http(monkeypatch):
    sent, answers = [], []

    def urlopen(req, timeout):
        sent.append(req)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(json.dumps(a).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent, answers


class TestKeys:
    def test_a_missing_key_refuses_before_any_request(self, monkeypatch, http):
        monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
        monkeypatch.setenv("ALPACA_PAPER_SECRET", "sec")
        with pytest.raises(RuntimeError, match="not set"):
            api.account()
        assert http[0] == []

    def test_a_still_encrypted_key_refuses(self, monkeypatch):
        monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "encrypted:abc")
        with pytest.raises(RuntimeError, match="still encrypted"):
            api.env("ALPACA_PAPER_KEY_ID")


class TestGet:
    def test_requests_go_only_to_the_paper_host_with_both_key_headers(self, keys, http):
        sent, answers = http
        answers.append({"status": "ACTIVE"})
        api.account()
        r = sent[0]
        assert r.full_url == "https://paper-api.alpaca.markets/v2/account" and r.get_method() == "GET"
        assert r.get_header("Apca-api-key-id") == "kid" and r.get_header("Apca-api-secret-key") == "sec"

    def test_a_path_outside_v2_is_refused_without_a_request(self, keys, http):
        with pytest.raises(ValueError, match="only /v2/"):
            api.get("/orders")
        assert http[0] == []

    def test_http_errors_propagate(self, keys, http):
        http[1].append(urllib.error.HTTPError("u", 403, "forbidden", {}, None))
        with pytest.raises(urllib.error.HTTPError):
            api.clock()


class TestAccountRules:
    def test_an_active_usd_unblocked_account_has_no_problems(self):
        assert api.account_problems({"status": "ACTIVE", "currency": "USD", "trading_blocked": False, "account_blocked": False}) == []

    def test_each_reason_to_refuse_is_listed(self):
        p = api.account_problems({"status": "ACCOUNT_UPDATED", "currency": "GBP", "trading_blocked": True, "account_blocked": True})
        assert len(p) == 4

    def test_equity_must_be_positive(self):
        assert api.equity_usd({"equity": "100000.5"}) == 100000.5
        with pytest.raises(ValueError, match="not positive"):
            api.equity_usd({"equity": "0"})


class TestPositions:
    def test_short_positions_come_back_negative(self, keys, http):
        http[1].append([{"symbol": "AAPL", "qty": "3.5", "side": "long"}, {"symbol": "XYZ", "qty": "2", "side": "short"}])
        assert api.positions() == {"AAPL": 3.5, "XYZ": -2.0}


class TestAsset:
    def test_a_share_class_symbol_is_accepted_and_junk_is_refused(self, keys, http):
        http[1].append({"symbol": "BRK.B", "tradable": True})
        assert api.asset("BRK.B")["tradable"]
        with pytest.raises(ValueError, match="bad symbol"):
            api.asset("brk/../x")
