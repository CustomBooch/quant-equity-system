"""Read-only client of the Trading 212 practice account (lab/t212/INTEGRATION_SPEC.md). GET only, to the practice host only (quant.execution.brokers.t212.api.DEMO), with the
read-only keys only (quant.execution.brokers.t212.api.auth("READ")); the one module that can send an order is quant.execution.engine (lab/tests/test_broker_boundary.py).

    ReadClient(clock=..., sleep=..., rng=...)      one client per process; the clock, the pause and the jitter source are parameters, never globals
    live_client()                                  the client the commands use: wall clock, real pauses, the fixed jitter seed
        .account_summary()                         cash and investments              documented limit 1 request / 5 s
        .positions(ticker=None)                    open positions                    1 / 1 s
        .dividends(ticker=None)                    paid dividends, every page        20 / 60 s
        .transactions(since_utc=None)              cash movements, every page        20 / 60 s
        .exports()                                 requested CSV reports (listed, never downloaded)   1 / 60 s
        .exchanges()                               exchanges and working schedules   1 / 30 s
        .instruments()                             the instrument list               1 / 50 s
    Each returns Pulled(data, responses): the typed records and the responses they came from, each with its journal record (quant.execution.brokers.t212.journal).

Failure behaviour (no silent fallback anywhere):
  * Retries: on HTTP 429 and 5xx only, at most Policy.max_attempts requests in all. A 429 that names its reset (x-ratelimit-reset) waits until then plus a
    small jitter; any other retry waits a full-jitter exponential backoff, uniform on [0, min(max_backoff, base x 2^(n-1))]. A reset further away than
    Policy.max_wait_s is not waited for: RateLimited is raised at once.
  * Pacing: when an endpoint's last response said no request is left in the period, the next request to it waits for the reset first.
  * Every other HTTP status raises its own error at once (BadRequest, Unauthorized, Forbidden: the key lacks the scope, NotFound, RequestTimeout,
    UnexpectedStatus); a network failure or timeout raises TransportError and is not retried; a redirect is refused (urllib would forward the Authorization
    header to the new location); a body that is not JSON of the documented schema raises InvalidResponse naming the JSON path and the journal record
    that holds the raw body.
  * Pagination follows the response's nextPagePath, which must continue the same endpoint on the same host; a repeated page or more than Policy.max_pages
    pages raises InvalidResponse.
  * Keys: a missing or still-encrypted read key refuses before any request (quant.execution.brokers.t212.api.auth). Keys are never logged, journaled or put in an error.

The pending-orders, order-by-id and historical-orders reads are documented but not called: lab/tests/test_broker_boundary.py refuses any module but the
engine that names the order endpoint, and that control is not changed here (INTEGRATION_REPORT.md, recommendation R1).
"""
from __future__ import annotations

import datetime as dt
import email.message
import http.client
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Generic, NoReturn, TypeVar

from quant.execution.brokers.t212 import journal, ratelimit, records
from quant.execution.brokers.t212.api import DEMO, auth
from quant.execution.brokers.t212.ratelimit import RateLimit, RateLimitHeaderError
from quant.execution.brokers.t212.schema import SchemaError, loads

T = TypeVar("T")
USER_AGENT = "quant-research-readonly"
JITTER_SEED = 212                                            # the jitter only spreads retries out; a fixed seed makes a run's pauses repeatable
_DEMO_PARTS = urllib.parse.urlsplit(DEMO)
API_PREFIX = _DEMO_PARTS.path                                # "/api/v0": the prefix of every nextPagePath


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A documented read: its name (as journaled), its path on the practice host, its documented rate limit, and whether it is paginated."""
    name: str
    path: str
    documented_limit: str
    paginated: bool = False


ACCOUNT_SUMMARY = Endpoint("account_summary", "/equity/account/summary", "1 req / 5s")
POSITIONS = Endpoint("positions", "/equity/positions", "1 req / 1s")
DIVIDENDS = Endpoint("dividends", "/equity/history/dividends", "20 req / 1m0s", paginated=True)
TRANSACTIONS = Endpoint("transactions", "/equity/history/transactions", "20 req / 1m0s", paginated=True)
EXPORTS = Endpoint("exports", "/equity/history/exports", "1 req / 1m0s")
EXCHANGES = Endpoint("exchanges", "/equity/metadata/exchanges", "1 req / 30s")
INSTRUMENTS = Endpoint("instruments", "/equity/metadata/instruments", "1 req / 50s")
# Documented reads deliberately absent: the pending-orders list, one pending order by id and the historical orders (see the module docstring), and the
# deprecated pies reads. Nothing here writes: the export request is a POST and is not implemented.
ENDPOINTS = {e.name: e for e in (ACCOUNT_SUMMARY, POSITIONS, DIVIDENDS, TRANSACTIONS, EXPORTS, EXCHANGES, INSTRUMENTS)}


@dataclass(frozen=True, slots=True, kw_only=True)
class Policy:
    """Timeouts, retries, waits and limits of the client (seconds, counts, bytes)."""
    timeout_s: float = 30.0                 # connect and each read, per request (urllib)
    max_attempts: int = 5                   # requests in all for one read, retries on 429 and 5xx included
    base_backoff_s: float = 1.0
    max_backoff_s: float = 30.0
    reset_margin_s: float = 0.5             # added to every wait for a rate-limit reset (clock skew)
    reset_jitter_s: float = 1.0             # a 429 that names its reset waits reset + margin + uniform[0, this]
    max_wait_s: float = 120.0               # longest wait for a reset; a longer one raises RateLimited instead
    page_limit: int = 50                    # items per page: the documented maximum
    max_pages: int = 1000
    max_body_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_pages < 1 or not 1 <= self.page_limit <= 50 or self.max_body_bytes < 1:
            raise ValueError(f"invalid policy: {self}")
        if min(self.timeout_s, self.base_backoff_s, self.max_backoff_s, self.max_wait_s) <= 0 or min(self.reset_margin_s, self.reset_jitter_s) < 0:
            raise ValueError(f"invalid policy: {self}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Response:
    """One successful response: what was asked, the body as received, its rate-limit state, when it arrived and its journal record (None if not journaled)."""
    endpoint: Endpoint
    path: str
    status: int
    body: bytes
    rate_limit: RateLimit | None
    received_utc: dt.datetime
    record_sha256: str | None


@dataclass(frozen=True, slots=True)
class Pulled(Generic[T]):
    """Typed records and the responses they were read from."""
    data: T
    responses: tuple[Response, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        """The journal records of the responses (the raw evidence of every number in `data`)."""
        return tuple(r.record_sha256 for r in self.responses if r.record_sha256 is not None)


@dataclass(frozen=True, slots=True, kw_only=True)
class Wait:
    """A pause the client made: before a request (rate limit spent) or before a retry (after `status`)."""
    endpoint: str
    reason: str
    seconds: float
    status: int | None


class ReadError(Exception):
    """A read that could not be completed."""

    def __init__(self, endpoint: Endpoint, path: str, message: str) -> None:
        super().__init__(f"{endpoint.name} ({path}): {message}")
        self.endpoint, self.path = endpoint, path


class TransportError(ReadError):
    """The request did not complete: connection refused or reset, name resolution, TLS, or a timeout. Not retried."""


class InvalidResponse(ReadError):
    """A 200 response that cannot be used: not JSON of the documented schema, another content type, too large, a malformed rate-limit header, or a next-page
    path that does not continue the read. `record_sha256` names the journal record holding the raw body, when it was journaled."""

    def __init__(self, endpoint: Endpoint, path: str, message: str, record_sha256: str | None = None) -> None:
        super().__init__(endpoint, path, message + (f" (raw body: journal record {record_sha256})" if record_sha256 else ""))
        self.record_sha256 = record_sha256


class HttpError(ReadError):
    """A response with an HTTP error status, after any retries."""

    def __init__(self, endpoint: Endpoint, path: str, status: int, excerpt: str, attempts: int) -> None:
        super().__init__(endpoint, path, f"HTTP {status} after {attempts} request(s): {excerpt}")
        self.status, self.excerpt, self.attempts = status, excerpt, attempts


class BadRequest(HttpError):
    """400: the reference documents it for bad filtering arguments."""


class Unauthorized(HttpError):
    """401: bad API key."""


class Forbidden(HttpError):
    """403: the key lacks the scope this read needs."""


class NotFound(HttpError):
    """404."""


class RequestTimeout(HttpError):
    """408: the server timed out (documented; not retried: only 429 and 5xx are)."""


class RateLimited(HttpError):
    """429 after every attempt, or with a reset too far away to wait for."""


class ServerError(HttpError):
    """5xx after every attempt."""


class UnexpectedStatus(HttpError):
    """Any other status, including a refused redirect."""


STATUS_ERRORS: dict[int, type[HttpError]] = {400: BadRequest, 401: Unauthorized, 403: Forbidden, 404: NotFound, 408: RequestTimeout, 429: RateLimited}


def error_for(status: int) -> type[HttpError]:
    return STATUS_ERRORS.get(status) or (ServerError if 500 <= status <= 599 else UnexpectedStatus)


def retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the practice host is the only host this layer calls, and urllib would send the Authorization header on to the new location."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: email.message.Message, newurl: str) -> NoReturn:
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused: only {DEMO} is called", headers, fp)


def excerpt(body: bytes, limit: int = 200) -> str:
    """The start of an error body as one line of text, for an error message."""
    return " ".join(body[: limit * 4].decode("utf-8", errors="replace").split())[:limit]


class ReadClient:
    """The read-only client. `clock` returns the current time (timezone-aware), `sleep` pauses, `rng` draws the retry jitter; responses are journaled to
    `journal_path` (None: not journaled, for a caller that only probes)."""

    def __init__(self, *, clock: Callable[[], dt.datetime], sleep: Callable[[float], None], rng: random.Random, policy: Policy | None = None,
                 journal_path: Path | None = journal.PATH) -> None:
        self._clock, self._sleep, self._rng = clock, sleep, rng
        self.policy = policy or Policy()
        self._journal = journal_path
        self._opener = urllib.request.build_opener(RefuseRedirects())
        self.rate_limits: dict[str, RateLimit] = {}             # the last rate-limit state each endpoint reported
        self.waits: list[Wait] = []                              # every pause made, in order

    def _now(self) -> dt.datetime:
        t = self._clock()
        if t.utcoffset() is None:
            raise ValueError("the clock returned a naive datetime")
        return t

    def _url(self, endpoint: Endpoint, path: str) -> str:
        """DEMO + path, for a path on the practice host only."""
        if not path.startswith("/") or path.startswith("//") or "://" in path or "\\" in path or any(c.isspace() or ord(c) < 32 for c in path):
            raise InvalidResponse(endpoint, path, "not a path on the practice host")
        url = DEMO + path
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme, parts.netloc) != (_DEMO_PARTS.scheme, _DEMO_PARTS.netloc) or not parts.path.startswith(API_PREFIX + "/"):
            raise InvalidResponse(endpoint, path, "not a path on the practice host")
        return url

    def _pause(self, endpoint: Endpoint, reason: str, seconds: float, status: int | None) -> None:
        self.waits.append(Wait(endpoint=endpoint.name, reason=reason, seconds=seconds, status=status))
        self._sleep(seconds)

    def _rate_limit(self, endpoint: Endpoint, path: str, headers: ratelimit.Headers | None) -> RateLimit | None:
        try:
            rl = ratelimit.parse(headers) if headers is not None else None
        except RateLimitHeaderError as e:
            raise InvalidResponse(endpoint, path, str(e)) from e
        if rl is not None:
            self.rate_limits[endpoint.name] = rl
        return rl

    def _retry_delay(self, endpoint: Endpoint, path: str, status: int, rl: RateLimit | None, attempt: int, body: bytes) -> float:
        if status == 429 and rl is not None:
            to_reset = rl.seconds_to_reset(self._now())
            if to_reset > self.policy.max_wait_s:
                raise RateLimited(endpoint, path, status, f"limit resets in {to_reset:.0f} s, beyond the {self.policy.max_wait_s:.0f} s wait allowed; "
                                                         f"{excerpt(body)}", attempt)
            return to_reset + self.policy.reset_margin_s + self._rng.uniform(0.0, self.policy.reset_jitter_s)
        return self._rng.uniform(0.0, min(self.policy.max_backoff_s, self.policy.base_backoff_s * 2 ** (attempt - 1)))

    def get(self, endpoint: Endpoint, path: str, pull: str | None = None, page: int = 0) -> Response:
        """One GET of `path` (query included) for `endpoint`, with the pacing, retries and checks of the module docstring; the response is journaled."""
        url = self._url(endpoint, path)
        pull = pull or f"{endpoint.name}@{self._now().isoformat()}"
        attempt = 0
        while True:
            attempt += 1
            wait = ratelimit.wait_before_next(self.rate_limits.get(endpoint.name), self._now())
            if wait > self.policy.max_wait_s:
                raise RateLimited(endpoint, path, 429, f"no request left until the reset in {wait:.0f} s, beyond the {self.policy.max_wait_s:.0f} s wait allowed",
                                  attempt - 1)
            if wait > 0:
                self._pause(endpoint, "rate limit spent: waiting for the reset", wait + self.policy.reset_margin_s, None)
            req = urllib.request.Request(url, headers={"Authorization": auth("READ"), "Accept": "application/json", "User-Agent": USER_AGENT})
            try:
                with self._opener.open(req, timeout=self.policy.timeout_s) as resp:
                    status, headers, body = resp.status, resp.headers, resp.read(self.policy.max_body_bytes + 1)
            except urllib.error.HTTPError as e:
                status, err_body = e.code, e.read(4096)
                e.close()
                rl = self._rate_limit(endpoint, path, e.headers)
                if not retryable(status) or attempt == self.policy.max_attempts:
                    raise error_for(status)(endpoint, path, status, excerpt(err_body) or str(e.reason), attempt) from None
                delay = self._retry_delay(endpoint, path, status, rl, attempt, err_body)
                self._pause(endpoint, f"retry after HTTP {status}", delay, status)
                continue
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                raise TransportError(endpoint, path, f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
            return self._accept(endpoint, path, pull, page, status, headers, body)

    def _accept(self, endpoint: Endpoint, path: str, pull: str, page: int, status: int, headers: email.message.Message, body: bytes) -> Response:
        if status != 200:
            raise UnexpectedStatus(endpoint, path, status, excerpt(body), 1)
        rl = self._rate_limit(endpoint, path, headers)
        if headers.get_content_type() != "application/json":
            raise InvalidResponse(endpoint, path, f"content type {headers.get_content_type()!r}, not application/json")
        if len(body) > self.policy.max_body_bytes:
            raise InvalidResponse(endpoint, path, f"body larger than {self.policy.max_body_bytes} bytes")
        received = self._now()
        record = None
        if self._journal is not None:
            try:
                record = journal.append(endpoint=endpoint.name, path=path, pull=pull, page=page, status=status, body=body, rate_limit=rl,
                                        received_utc=received, journal=self._journal)
            except journal.JournalError as e:
                raise InvalidResponse(endpoint, path, str(e)) from e
        return Response(endpoint=endpoint, path=path, status=status, body=body, rate_limit=rl, received_utc=received, record_sha256=record)

    def _parse(self, r: Response, parse: Callable[[object], T]) -> T:
        try:
            return parse(loads(r.body))
        except SchemaError as e:
            raise InvalidResponse(r.endpoint, r.path, f"not the documented schema: {e}", r.record_sha256) from e

    def _single(self, endpoint: Endpoint, path: str, parse: Callable[[object], T]) -> Pulled[T]:
        r = self.get(endpoint, path)
        return Pulled(self._parse(r, parse), (r,))

    def _next_path(self, endpoint: Endpoint, next_page_path: str, r: Response) -> str:
        """The request path of the page `next_page_path` names; it must continue `endpoint` on the practice host."""
        if not next_page_path.startswith(f"{API_PREFIX}{endpoint.path}?") or "#" in next_page_path:
            raise InvalidResponse(endpoint, r.path, f"nextPagePath {next_page_path!r} does not continue {API_PREFIX}{endpoint.path}", r.record_sha256)
        return next_page_path[len(API_PREFIX):]

    def _paginate(self, endpoint: Endpoint, first: str, parse: Callable[[object], records.Page[T]]) -> Pulled[tuple[T, ...]]:
        pull = f"{endpoint.name}@{self._now().isoformat()}"
        path, seen = first, {first}
        items: list[T] = []
        responses: list[Response] = []
        for page in range(self.policy.max_pages):
            r = self.get(endpoint, path, pull, page)
            p = self._parse(r, parse)
            items.extend(p.items)
            responses.append(r)
            if p.next_page_path is None:
                return Pulled(tuple(items), tuple(responses))
            path = self._next_path(endpoint, p.next_page_path, r)
            if path in seen:
                raise InvalidResponse(endpoint, r.path, f"nextPagePath {p.next_page_path!r} repeats a page already read", r.record_sha256)
            seen.add(path)
        raise InvalidResponse(endpoint, first, f"more than {self.policy.max_pages} pages")

    def _query(self, path: str, params: list[tuple[str, str]]) -> str:
        return f"{path}?{urllib.parse.urlencode(params)}" if params else path

    def account_summary(self) -> Pulled[records.AccountSummary]:
        return self._single(ACCOUNT_SUMMARY, ACCOUNT_SUMMARY.path, records.parse_account_summary)

    def positions(self, ticker: str | None = None) -> Pulled[tuple[records.Position, ...]]:
        return self._single(POSITIONS, self._query(POSITIONS.path, [("ticker", ticker)] if ticker else []), records.parse_positions)

    def dividends(self, ticker: str | None = None) -> Pulled[tuple[records.DividendItem, ...]]:
        params = [("limit", str(self.policy.page_limit))] + ([("ticker", ticker)] if ticker else [])
        return self._paginate(DIVIDENDS, self._query(DIVIDENDS.path, params), records.parse_dividends_page)

    def transactions(self, since_utc: dt.datetime | None = None) -> Pulled[tuple[records.Transaction, ...]]:
        if since_utc is not None and since_utc.utcoffset() is None:
            raise ValueError("since_utc must be timezone-aware")
        params = [("limit", str(self.policy.page_limit))] + ([("time", since_utc.astimezone(dt.UTC).isoformat())] if since_utc else [])
        return self._paginate(TRANSACTIONS, self._query(TRANSACTIONS.path, params), records.parse_transactions_page)

    def exports(self) -> Pulled[tuple[records.ExportReport, ...]]:
        return self._single(EXPORTS, EXPORTS.path, records.parse_exports)

    def exchanges(self) -> Pulled[tuple[records.Exchange, ...]]:
        return self._single(EXCHANGES, EXCHANGES.path, records.parse_exchanges)

    def instruments(self) -> Pulled[tuple[records.TradableInstrument, ...]]:
        return self._single(INSTRUMENTS, INSTRUMENTS.path, records.parse_instruments)


def live_client(journal_path: Path | None = journal.PATH, policy: Policy | None = None) -> ReadClient:
    """The client the commands use: the wall clock (UTC), real pauses, the fixed jitter seed, responses journaled to `journal_path`."""
    rng = random.Random(JITTER_SEED)                       # noqa: S311 - spreads retries in time; nothing secret is drawn from it
    return ReadClient(clock=lambda: dt.datetime.now(dt.UTC), sleep=time.sleep, rng=rng, policy=policy, journal_path=journal_path)
