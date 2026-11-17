"""quant.execution.brokers.t212.ratelimit: the documented rate-limit headers parsed strictly, and the wait before the next request to the same endpoint. Pure functions."""
from __future__ import annotations

import datetime as dt
import email.message

import pytest

from quant.execution.brokers.t212 import ratelimit as RL

NOW = dt.datetime(2026, 10, 27, 14, 30, 0, tzinfo=dt.UTC)


def headers(**values: str) -> email.message.Message:
    """Response headers as http.client delivers them (case-insensitive), from x-ratelimit-<name>=value pairs."""
    m = email.message.Message()
    for name, value in values.items():
        m[f"X-RateLimit-{name.capitalize()}"] = value
    return m


FULL = {"limit": "20", "period": "60", "remaining": "0", "reset": str(int(NOW.timestamp()) + 42), "used": "20"}


def test_the_five_headers_become_one_record_whatever_their_case():
    r = RL.parse(headers(**FULL))
    assert r == RL.RateLimit(limit=20, period_s=60, remaining=0, reset_utc=NOW + dt.timedelta(seconds=42), used=20)
    assert r is not None and r.seconds_to_reset(NOW) == 42.0 and r.headroom() == 0.0


def test_a_response_without_rate_limit_headers_has_no_record():
    assert RL.parse(email.message.Message()) is None


def test_a_partial_set_of_headers_is_refused_and_names_what_is_missing():
    with pytest.raises(RL.RateLimitHeaderError, match=r"incomplete: \['x-ratelimit-used'\] missing"):
        RL.parse(headers(**{k: v for k, v in FULL.items() if k != "used"}))


@pytest.mark.parametrize("value", ["-1", "1.5", "soon", ""])
def test_a_header_that_is_not_a_non_negative_integer_is_refused(value):
    with pytest.raises(RL.RateLimitHeaderError, match="x-ratelimit-remaining is not a non-negative integer"):
        RL.parse(headers(**{**FULL, "remaining": value}))


def test_the_next_request_waits_for_the_reset_only_when_no_request_is_left():
    spent = RL.RateLimit(limit=1, period_s=5, remaining=0, reset_utc=NOW + dt.timedelta(seconds=3), used=1)
    left = RL.RateLimit(limit=20, period_s=60, remaining=4, reset_utc=NOW + dt.timedelta(seconds=30), used=16)
    assert RL.wait_before_next(spent, NOW) == 3.0 and RL.wait_before_next(left, NOW) == 0.0 and RL.wait_before_next(None, NOW) == 0.0
    assert RL.wait_before_next(spent, NOW + dt.timedelta(seconds=10)) == 0.0          # the reset has passed


def test_the_json_form_reads_back_to_the_same_record():
    r = RL.RateLimit(limit=1, period_s=5, remaining=0, reset_utc=NOW, used=1)
    assert RL.RateLimit.from_json(r.as_json()) == r and r.headroom() == 0.0
    assert RL.RateLimit(limit=0, period_s=5, remaining=0, reset_utc=NOW, used=0).headroom() == 0.0
