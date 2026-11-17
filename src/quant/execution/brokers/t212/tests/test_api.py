"""quant.execution.brokers.t212.api: read-only access to the practice account. HTTP is the boundary: urlopen is replaced, nothing reaches the network."""
from __future__ import annotations

import base64
import io
import urllib.error
import urllib.request

import pytest

from quant.execution.brokers.t212 import api


@pytest.fixture()
def keys(monkeypatch):
    for k, v in {"T212_READ_API_KEY": "k1", "T212_READ_SECRET_KEY": "s1", "T212_FULL_API_KEY": "k2", "T212_FULL_SECRET_KEY": "s2"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def http(monkeypatch):
    """Records each request and answers with the next body (or raises it if it is an exception)."""
    sent, answers = [], []

    def urlopen(req, timeout):
        sent.append(req)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(a)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent, answers


def test_values_come_from_the_environment_and_an_absent_one_is_empty(keys, monkeypatch):
    monkeypatch.delenv("T212_MISSING", raising=False)
    assert api.env("T212_READ_API_KEY") == "k1" and api.env("T212_MISSING") == ""


def test_a_value_still_encrypted_by_dotenvx_refuses(monkeypatch):
    monkeypatch.setenv("T212_READ_API_KEY", "encrypted:BOx8ab")
    with pytest.raises(RuntimeError, match="still encrypted: start the command with `dotenvx run"):
        api.env("T212_READ_API_KEY")


def test_a_missing_key_refuses_before_any_request(monkeypatch, http):
    sent, _ = http
    monkeypatch.setenv("T212_READ_API_KEY", "k1")
    monkeypatch.delenv("T212_READ_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="T212_READ_SECRET_KEY is not set"):
        api.get("/equity/positions")
    assert sent == []


def test_read_and_full_keys_build_different_basic_headers(keys):
    assert api.auth("READ") == "Basic " + base64.b64encode(b"k1:s1").decode()
    assert api.auth("FULL") == "Basic " + base64.b64encode(b"k2:s2").decode()


def test_get_calls_the_practice_host_with_the_read_only_keys_and_parses_json(keys, http):
    sent, answers = http
    answers.append(b'{"cash": 10000}')
    assert api.get("/equity/account/summary") == {"cash": 10000}
    req = sent[0]
    assert req.full_url == "https://demo.trading212.com/api/v0/equity/account/summary" and req.get_method() == "GET"
    assert req.get_header("Authorization") == api.auth("READ")


@pytest.mark.parametrize("body", [
    b'[{"ticker": "A_US_EQ", "quantity": 2.5}, {"instrument": {"ticker": "B_US_EQ"}, "quantity": "1"}]',
    b'{"items": [{"ticker": "A_US_EQ", "quantity": 2.5}, {"instrument": {"ticker": "B_US_EQ"}, "quantity": 1}]}',
])
def test_positions_map_tickers_to_quantities_in_either_response_shape(keys, http, body):
    http[1].append(body)
    assert api.positions() == {"A_US_EQ": 2.5, "B_US_EQ": 1.0} and http[0][0].full_url.endswith("/equity/positions")


def test_a_position_without_a_quantity_raises_instead_of_counting_as_zero(keys, http):
    http[1].append(b'[{"ticker": "A_US_EQ"}]')
    with pytest.raises(KeyError):
        api.positions()


def test_an_http_error_reaches_the_caller(keys, http):
    _, answers = http
    answers.append(urllib.error.HTTPError(api.DEMO, 401, "Unauthorized", {}, None))
    with pytest.raises(urllib.error.HTTPError):
        api.get("/equity/positions")
