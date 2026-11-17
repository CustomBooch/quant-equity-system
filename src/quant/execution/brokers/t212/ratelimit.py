"""Trading 212 rate-limit headers, which the reference says every response carries, and the wait they imply. Limits apply per account and per endpoint (for example
1 request per 5 s for the account summary, 20 per minute for each paginated history read), whichever key or address makes the request.

    x-ratelimit-limit       requests allowed in the period
    x-ratelimit-period      length of the period, seconds
    x-ratelimit-remaining   requests left in the current period
    x-ratelimit-reset       Unix time, seconds, at which the limit fully resets
    x-ratelimit-used        requests already made in the period

A response carries all five or none; a partial or malformed set is refused (RateLimitHeaderError), never guessed.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Protocol

HEADERS = ("x-ratelimit-limit", "x-ratelimit-period", "x-ratelimit-remaining", "x-ratelimit-reset", "x-ratelimit-used")
INTEGER = re.compile(r"\d{1,12}")                           # a non-negative decimal integer (12 digits: Unix seconds for ten thousand years and more)


class Headers(Protocol):
    """What a response's headers offer: a case-insensitive get (http.client.HTTPMessage does this)."""

    def get(self, name: str) -> str | None: ...


class RateLimitHeaderError(ValueError):
    """Rate-limit headers that are partly present or not non-negative integers."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimit:
    """The rate-limit state an endpoint reported with one response."""
    limit: int
    period_s: int
    remaining: int
    reset_utc: dt.datetime
    used: int

    def seconds_to_reset(self, now_utc: dt.datetime) -> float:
        """Seconds from `now_utc` to the reset (0 once it has passed)."""
        return max(0.0, (self.reset_utc - now_utc).total_seconds())

    def headroom(self) -> float:
        """Share of the period's requests still available (0 when the limit is 0)."""
        return self.remaining / self.limit if self.limit else 0.0

    def as_json(self) -> dict[str, int | str]:
        return {"limit": self.limit, "period_s": self.period_s, "remaining": self.remaining, "reset_utc": self.reset_utc.isoformat(), "used": self.used}

    @classmethod
    def from_json(cls, value: dict[str, int | str]) -> RateLimit:
        """The record `as_json` wrote (quant.execution.brokers.t212.journal stores it with each response)."""
        reset = dt.datetime.fromisoformat(str(value["reset_utc"]))
        return cls(limit=int(value["limit"]), period_s=int(value["period_s"]), remaining=int(value["remaining"]), reset_utc=reset, used=int(value["used"]))


def parse(headers: Headers) -> RateLimit | None:
    """The rate-limit state in `headers`; None when the response carries none of the five headers. RateLimitHeaderError when some are missing or any is not a
    non-negative integer."""
    raw = {h: headers.get(h) for h in HEADERS}
    present = [h for h, v in raw.items() if v is not None]
    if not present:
        return None
    if missing := [h for h in HEADERS if raw[h] is None]:
        raise RateLimitHeaderError(f"rate-limit headers incomplete: {missing} missing, {present} present")
    values: dict[str, int] = {}
    for h, v in raw.items():
        text = str(v).strip()
        if not INTEGER.fullmatch(text):
            raise RateLimitHeaderError(f"{h} is not a non-negative integer: {text!r}")
        values[h] = int(text)
    return RateLimit(limit=values["x-ratelimit-limit"], period_s=values["x-ratelimit-period"], remaining=values["x-ratelimit-remaining"],
                     reset_utc=dt.datetime.fromtimestamp(values["x-ratelimit-reset"], dt.UTC), used=values["x-ratelimit-used"])


def wait_before_next(last: RateLimit | None, now_utc: dt.datetime) -> float:
    """Seconds to wait before the next request to the endpoint that reported `last`: until its reset if the period's requests are used up, otherwise none."""
    if last is None or last.remaining > 0:
        return 0.0
    return last.seconds_to_reset(now_utc)
