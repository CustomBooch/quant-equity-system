"""quant.execution.brokers.t212.client: the read-only client. Boundaries replaced: the broker's HTTP (the opener urllib.request.build_opener returns; the client builds its own so
that it can refuse redirects), the clock and the pause (parameters). Responses are real urllib response and HTTPError objects; the journal is a real file."""
from __future__ import annotations

import datetime as dt
import email.message
import io
import random
import socket
import urllib.error
import urllib.request

import pytest

from quant.execution.brokers.t212 import api
from quant.execution.brokers.t212 import client as C
from quant.execution.brokers.t212 import journal as J
from quant.execution.brokers.t212 import schema as S
from quant.execution.brokers.t212.tests import factories as F

T0 = F.T0


def retry_delays(monkeypatch: pytest.MonkeyPatch, seed: int) -> list[float]:
    """The pauses of one read that meets two server errors, with the jitter drawn from random.Random(seed)."""
    broker = F.Broker().install(monkeypatch)
    broker.answers += [F.status(500), F.status(500), F.ok([])]
    clock = F.Clock()
    c = C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(seed), journal_path=None)
    c.positions()
    return [w.seconds for w in c.waits]


@pytest.fixture()
def make(tmp_path, clock, keys, broker):
    def make(**policy: object) -> C.ReadClient:
        return C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(7), policy=C.Policy(**policy) if policy else None,
                            journal_path=tmp_path / "journal.jsonl")
    return make


def test_the_network_guard_refuses_a_real_connection():
    with pytest.raises(RuntimeError, match="tried to open a network connection"):
        socket.create_connection(("demo.trading212.com", 443), timeout=1)


class TestRequests:
    def test_a_read_is_one_get_to_the_practice_host_with_the_read_only_keys_and_a_timeout(self, make, broker, tmp_path):
        broker.answers.append(F.ok(F.account_summary(), rate=(1, 5, 0, 5)))
        pulled = make(timeout_s=12.5).account_summary()
        (req, timeout), = broker.requests
        assert (req.get_method(), req.full_url, timeout) == ("GET", api.DEMO + "/equity/account/summary", 12.5) and req.data is None
        assert req.get_header("Authorization") == api.auth("READ") != api.auth("FULL") and req.get_header("Accept") == "application/json"
        assert pulled.data.account_currency == "GBP" and pulled.responses[0].rate_limit.remaining == 0

    def test_the_opener_refuses_redirects(self, make, broker):
        make()
        assert len(broker.handlers) == 1 and isinstance(broker.handlers[0], C.RefuseRedirects)

    def test_a_redirect_is_refused_instead_of_being_followed_with_the_keys(self):
        req = urllib.request.Request(api.DEMO + "/equity/positions")
        with pytest.raises(urllib.error.HTTPError, match="redirect to https://elsewhere.example/ refused") as e:
            C.RefuseRedirects().redirect_request(req, io.BytesIO(b""), 302, "Found", email.message.Message(), "https://elsewhere.example/")
        assert e.value.code == 302

    def test_a_refused_redirect_reaches_the_caller_as_an_unexpected_status_without_retry(self, make, broker):
        broker.answers.append(F.status(302, b""))
        with pytest.raises(C.UnexpectedStatus, match="HTTP 302 after 1 request"):
            make().positions()
        assert len(broker.requests) == 1

    def test_a_success_status_other_than_200_is_refused_unretried_and_not_journaled(self, make, broker, tmp_path):
        broker.answers.append((204, F.headers(), b""))
        with pytest.raises(C.UnexpectedStatus, match="HTTP 204 after 1 request"):
            make().account_summary()
        assert len(broker.requests) == 1 and J.entries(tmp_path / "journal.jsonl") == []

    def test_a_missing_read_key_refuses_before_any_request(self, make, broker, monkeypatch):
        c = make()
        monkeypatch.delenv("T212_READ_SECRET_KEY")
        with pytest.raises(RuntimeError, match="T212_READ_SECRET_KEY is not set"):
            c.positions()
        assert broker.requests == []

    def test_each_response_is_journaled_as_received_before_it_is_parsed(self, make, broker, tmp_path):
        broker.answers.append(F.ok([F.position("AAA_US_EQ", 2.0)], rate=(1, 1, 0, 1)))
        pulled = make().positions(ticker="AAA_US_EQ")
        (e,) = J.entries(tmp_path / "journal.jsonl")
        assert pulled.sources == (e.record_sha256,) and e.body == F.body([F.position("AAA_US_EQ", 2.0)]) and e.endpoint == "positions"
        assert e.path == "/equity/positions?ticker=AAA_US_EQ" and e.rate_limit == pulled.responses[0].rate_limit and broker.requests[0][0].full_url.endswith(e.path)

    def test_a_client_that_does_not_journal_records_nothing(self, keys, broker, clock, tmp_path):
        broker.answers.append(F.ok(F.account_summary()))
        pulled = C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(1), journal_path=None).account_summary()
        assert pulled.sources == () and pulled.responses[0].record_sha256 is None and not (tmp_path / "journal.jsonl").exists()

    @pytest.mark.parametrize("path", ["equity/positions", "//evil.example/x", "https://evil.example/api/v0/x", "/equity/positions x", "/a\\b"])
    def test_a_path_that_is_not_on_the_practice_host_is_refused_before_any_request(self, make, broker, path):
        with pytest.raises(C.InvalidResponse, match="not a path on the practice host"):
            make().get(C.POSITIONS, path)
        assert broker.requests == []

    def test_a_naive_clock_is_refused(self, keys, broker, tmp_path):
        c = C.ReadClient(clock=lambda: dt.datetime(2026, 10, 27), sleep=lambda s: None, rng=random.Random(1), journal_path=tmp_path / "j.jsonl")
        with pytest.raises(ValueError, match="naive datetime"):
            c.positions()

    @pytest.mark.parametrize("bad", [{"max_attempts": 0}, {"page_limit": 51}, {"timeout_s": 0}, {"reset_jitter_s": -1}])
    def test_an_impossible_policy_is_refused(self, bad):
        with pytest.raises(ValueError, match="invalid policy"):
            C.Policy(**bad)


class TestRetries:
    def test_a_429_waits_for_the_reset_it_names_then_succeeds(self, make, broker, clock):
        broker.answers += [F.status(429, rate=(1, 5, 0, 4.0)), F.ok([F.position()], rate=(1, 1, 0, 1))]
        c = make()
        assert len(c.positions().data) == 1 and len(broker.requests) == 2
        (wait,) = c.waits
        assert wait.status == 429 and 4.0 + 0.5 <= wait.seconds <= 4.0 + 0.5 + 1.0 and clock.sleeps == [wait.seconds]

    def test_server_errors_are_retried_with_full_jitter_exponential_backoff(self, make, broker):
        broker.answers += [F.status(503), F.status(502), F.status(500), F.ok([], rate=(1, 1, 0, 1))]
        c = make(base_backoff_s=2.0, max_backoff_s=5.0)
        c.positions()
        bounds = [2.0, 4.0, 5.0]                                                  # base x 2^(n-1), capped at max_backoff_s
        assert [w.status for w in c.waits] == [503, 502, 500] and all(0.0 <= w.seconds <= b for w, b in zip(c.waits, bounds, strict=True))

    def test_the_jitter_is_drawn_from_the_injected_source_so_a_run_can_be_repeated(self, keys, monkeypatch):
        assert retry_delays(monkeypatch, seed=42) == retry_delays(monkeypatch, seed=42) != retry_delays(monkeypatch, seed=43)

    def test_a_429_on_every_attempt_raises_rate_limited_after_the_last(self, make, broker):
        broker.answers += [F.status(429)] * 3
        with pytest.raises(C.RateLimited, match="HTTP 429 after 3 request"):
            make(max_attempts=3).positions()
        assert len(broker.requests) == 3

    def test_a_reset_too_far_away_is_not_waited_for(self, make, broker, clock):
        broker.answers.append(F.status(429, rate=(20, 60, 0, 600.0)))
        with pytest.raises(C.RateLimited, match="resets in 600 s, beyond the 120 s wait allowed"):
            make().dividends()
        assert clock.sleeps == []

    def test_server_errors_on_every_attempt_raise_server_error(self, make, broker):
        broker.answers += [F.status(500, b"Internal error, please retry")] * 2
        with pytest.raises(C.ServerError, match="HTTP 500 after 2 request.*Internal error, please retry"):
            make(max_attempts=2).exchanges()

    @pytest.mark.parametrize("code,error", [(400, C.BadRequest), (401, C.Unauthorized), (403, C.Forbidden), (404, C.NotFound), (408, C.RequestTimeout),
                                            (418, C.UnexpectedStatus)])
    def test_other_errors_raise_their_own_error_at_once_without_retry(self, make, broker, code, error):
        broker.answers.append(F.status(code, b"Scope( portfolio ) missing for API key"))
        with pytest.raises(error, match=f"HTTP {code} after 1 request"):
            make().positions()
        assert len(broker.requests) == 1

    @pytest.mark.parametrize("failure", [urllib.error.URLError("Name or service not known"), TimeoutError("timed out"), ConnectionResetError("reset by peer")])
    def test_a_network_failure_or_timeout_raises_transport_error_without_retry(self, make, broker, failure):
        broker.answers.append(failure)
        with pytest.raises(C.TransportError, match=type(failure).__name__):
            make().positions()
        assert len(broker.requests) == 1

    def test_an_error_body_never_carries_the_keys_into_the_message(self, make, broker):
        broker.answers.append(F.status(401, b"Bad API key"))
        with pytest.raises(C.Unauthorized) as e:
            make().positions()
        assert "k-read" not in str(e.value) and "s-read" not in str(e.value) and api.auth("READ") not in str(e.value)


class TestPacing:
    def test_a_spent_rate_limit_delays_the_next_request_to_the_same_endpoint_until_the_reset(self, make, broker, clock):
        broker.answers += [F.ok(F.account_summary(), rate=(1, 5, 0, 3.0)), F.ok(F.account_summary(), rate=(1, 5, 0, 5.0))]
        c = make()
        c.account_summary()
        c.account_summary()
        assert [(w.reason, w.seconds) for w in c.waits] == [("rate limit spent: waiting for the reset", 3.5)]

    def test_another_endpoint_is_not_delayed_by_a_spent_limit(self, make, broker):
        broker.answers += [F.ok(F.account_summary(), rate=(1, 5, 0, 3.0)), F.ok([], rate=(1, 1, 0, 1.0))]
        c = make()
        c.account_summary()
        c.positions()
        assert c.waits == []

    def test_a_reset_beyond_the_allowed_wait_refuses_the_next_request(self, make, broker):
        broker.answers.append(F.ok(F.account_summary(), rate=(1, 5, 0, 900.0)))
        c = make()
        c.account_summary()
        with pytest.raises(C.RateLimited, match="no request left until the reset in 900 s"):
            c.account_summary()
        assert len(broker.requests) == 1


class TestPagination:
    NEXT = "/api/v0/equity/history/dividends?limit=50&cursor=1760346100000"

    def test_every_page_is_read_by_following_next_page_path_and_journaled_as_one_pull(self, make, broker, tmp_path):
        broker.answers += [F.ok(F.page([F.dividend("AAA_US_EQ")], self.NEXT), rate=(20, 60, 19, 60)), F.ok(F.page([F.dividend("BBB_US_EQ")]), rate=(20, 60, 18, 60))]
        pulled = make().dividends()
        assert [d.ticker for d in pulled.data] == ["AAA_US_EQ", "BBB_US_EQ"]
        assert [r.full_url for r, _ in broker.requests] == [api.DEMO + "/equity/history/dividends?limit=50", api.DEMO + "/equity/history/dividends?limit=50&cursor=1760346100000"]
        pages = J.latest_pull("dividends", tmp_path / "journal.jsonl")
        assert [p.page for p in pages] == [0, 1] and len({p.pull for p in pages}) == 1 and pulled.sources == tuple(p.record_sha256 for p in pages)

    def test_transactions_start_from_a_time_given_with_its_offset(self, make, broker):
        broker.answers.append(F.ok(F.page([F.transaction()])))
        make().transactions(since_utc=dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC))
        assert broker.requests[0][0].full_url == api.DEMO + "/equity/history/transactions?limit=50&time=2026-10-01T09%3A00%3A00%2B00%3A00"
        with pytest.raises(ValueError, match="timezone-aware"):
            make().transactions(since_utc=dt.datetime(2026, 10, 1))

    @pytest.mark.parametrize("bad", ["https://evil.example/api/v0/equity/history/dividends?cursor=1", "/api/v0/equity/positions?cursor=1",
                                     "/api/v0/equity/history/dividends?cursor=1#x"])
    def test_a_next_page_that_does_not_continue_the_read_is_refused_before_it_is_requested(self, make, broker, bad):
        broker.answers.append(F.ok(F.page([F.dividend()], bad)))
        with pytest.raises(C.InvalidResponse, match="does not continue /api/v0/equity/history/dividends"):
            make().dividends()
        assert len(broker.requests) == 1

    def test_a_next_page_that_repeats_a_page_already_read_is_refused(self, make, broker):
        broker.answers += [F.ok(F.page([F.dividend()], self.NEXT)), F.ok(F.page([F.dividend()], self.NEXT))]
        with pytest.raises(C.InvalidResponse, match="repeats a page already read"):
            make().dividends()

    def test_more_pages_than_allowed_are_refused(self, make, broker):
        broker.answers += [F.ok(F.page([F.dividend()], f"{self.NEXT}{i}")) for i in range(3)]
        with pytest.raises(C.InvalidResponse, match="more than 2 pages"):
            make(max_pages=2).dividends()


class TestResponses:
    def test_a_body_outside_the_documented_schema_is_refused_with_its_path_and_its_journal_record(self, make, broker, tmp_path):
        broker.answers.append(F.ok([{**F.position(), "quantity": "2"}]))
        with pytest.raises(C.InvalidResponse, match=r"\$\[0\]\.quantity: expected number, got string") as e:
            make().positions()
        assert isinstance(e.value.__cause__, S.WrongType) and e.value.record_sha256 == J.entries(tmp_path / "journal.jsonl")[0].record_sha256

    def test_a_body_that_is_not_json_is_refused(self, make, broker):
        broker.answers.append((200, F.headers(), b"<html>maintenance</html>"))
        with pytest.raises(C.InvalidResponse, match="body is not JSON"):
            make().account_summary()

    def test_another_content_type_is_refused_and_not_journaled(self, make, broker, tmp_path):
        broker.answers.append((200, F.headers(content_type="text/html"), b"[]"))
        with pytest.raises(C.InvalidResponse, match="content type 'text/html'"):
            make().positions()
        assert not (tmp_path / "journal.jsonl").exists()

    def test_a_body_larger_than_allowed_is_refused(self, make, broker):
        broker.answers.append(F.ok([F.tradable_instrument()] * 20))
        with pytest.raises(C.InvalidResponse, match="body larger than 100 bytes"):
            make(max_body_bytes=100).instruments()

    def test_a_body_that_is_not_utf8_is_refused_and_not_journaled(self, make, broker, tmp_path):
        broker.answers.append((200, F.headers(), b"\xff\xfe[]"))
        with pytest.raises(C.InvalidResponse, match="not UTF-8"):
            make().positions()
        assert not (tmp_path / "journal.jsonl").exists()

    def test_partial_rate_limit_headers_are_refused(self, make, broker):
        h = F.headers()
        h["x-ratelimit-limit"] = "1"
        broker.answers.append((200, h, b"[]"))
        with pytest.raises(C.InvalidResponse, match="rate-limit headers incomplete"):
            make().positions()

    def test_every_documented_read_parses_into_its_records(self, make, broker):
        broker.answers += [F.ok([F.export_report("Queued")]), F.ok([F.exchange()]), F.ok([F.tradable_instrument()]), F.ok(F.page([]))]
        c = make()
        assert c.exports().data[0].status == "Queued" and c.exchanges().data[0].name == "NASDAQ" and c.instruments().data[0].ticker == "AAA_US_EQ"
        assert c.transactions().data == ()
        assert {r.get_method() for r, _ in broker.requests} == {"GET"} and all(r.full_url.startswith(api.DEMO + "/") for r, _ in broker.requests)


def test_the_live_client_reads_the_wall_clock_in_utc_and_journals_where_asked(broker, tmp_path):
    c = C.live_client(journal_path=tmp_path / "j.jsonl")
    now = c._clock()
    assert now.tzinfo is dt.UTC and abs((dt.datetime.now(dt.UTC) - now).total_seconds()) < 60 and c._journal == tmp_path / "j.jsonl"
    assert C.live_client()._journal == J.PATH and isinstance(broker.handlers[0], C.RefuseRedirects)
