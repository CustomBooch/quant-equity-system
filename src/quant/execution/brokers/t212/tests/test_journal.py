"""quant.execution.brokers.t212.journal: the append-only, hash-chained record of read responses. Real files in tmp_path; the clock is a parameter."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from quant.execution.brokers.t212 import journal as J
from quant.execution.brokers.t212.ratelimit import RateLimit

T0 = dt.datetime(2026, 10, 27, 14, 30, 0, tzinfo=dt.UTC)


def add(path, endpoint="positions", body=b"[]", pull=None, page=0, minutes=0, rate_limit=None):
    return J.append(endpoint=endpoint, path=f"/equity/{endpoint}", pull=pull or f"{endpoint}@{minutes}", page=page, status=200, body=body, rate_limit=rate_limit,
                    received_utc=T0 + dt.timedelta(minutes=minutes), journal=path)


@pytest.fixture()
def journal(tmp_path):
    return tmp_path / "t212_journal" / "journal.jsonl"


def test_each_response_is_stored_as_received_and_identified_by_the_hash_of_its_line(journal):
    rl = RateLimit(limit=1, period_s=1, remaining=0, reset_utc=T0, used=1)
    rid = add(journal, body='[{"q": "é"}]'.encode(), rate_limit=rl)
    (e,) = J.entries(journal)
    assert e.record_sha256 == rid == J.sha(journal.read_bytes().splitlines()[0]) and e.body == '[{"q": "é"}]'.encode()
    assert (e.endpoint, e.page, e.status, e.rate_limit, e.received_utc, e.line_no) == ("positions", 0, 200, rl, T0, 1)


def test_records_are_chained_and_the_chain_verifies(journal):
    first = add(journal, minutes=0)
    add(journal, minutes=1)
    v = J.verify(journal)
    assert v == {"records": 2, "broken": [], "bad_body": [], "head": J.sha(journal.read_bytes().splitlines()[1]), "ok": True}
    assert json.loads(journal.read_bytes().splitlines()[1])["prev"] == first


def test_an_empty_or_absent_journal_verifies_and_has_no_records(journal):
    assert J.verify(journal) == {"records": 0, "broken": [], "bad_body": [], "head": None, "ok": True} and J.entries(journal) == []
    assert J.latest("positions", journal) is None and J.latest_pull("positions", journal) == ()


@pytest.mark.parametrize("tamper,broken,bad", [
    (lambda ls: [ls[0], ls[2]], [2], []),                                                       # a record deleted
    (lambda ls: [ls[0], ls[1].replace(b'"page":0', b'"page":5'), ls[1], ls[2]], [3], []),       # one inserted, chained to the line before: the next breaks
    (lambda ls: [ls[1], ls[0], ls[2]], [1, 2, 3], []),                                          # reordered
    (lambda ls: [ls[0], ls[1].replace(b'"page":0', b'"page":1'), ls[2]], [3], []),              # a field edited: the next record no longer chains
    (lambda ls: [ls[0], ls[1].replace(b'"body":"[]"', b'"body":"[1]"'), ls[2]], [3], [2]),      # a body edited without its hash
    (lambda ls: [ls[0], b"not json", ls[2]], [2, 3], []),
])
def test_every_kind_of_tampering_is_reported_by_line(journal, tamper, broken, bad):
    for m in range(3):
        add(journal, minutes=m)
    journal.write_bytes(b"\n".join(tamper(journal.read_bytes().splitlines())) + b"\n")
    v = J.verify(journal)
    assert (v["broken"], v["bad_body"], v["ok"]) == (broken, bad, False)


def test_a_journal_that_fails_verification_is_not_read(journal):
    add(journal)
    journal.write_bytes(journal.read_bytes() + b'{"utc": "x"}\n')
    with pytest.raises(J.JournalError, match=r"fails verification: broken line\(s\) \[2\]"):
        J.entries(journal)


def test_a_body_that_is_not_utf8_or_a_naive_time_is_refused_and_nothing_is_written(journal):
    with pytest.raises(J.JournalError, match="body is not UTF-8"):
        add(journal, body=b"\xff\xfe")
    with pytest.raises(J.JournalError, match="timezone-aware"):
        J.append(endpoint="positions", path="/p", pull="p", page=0, status=200, body=b"[]", rate_limit=None, received_utc=T0.replace(tzinfo=None), journal=journal)
    assert not journal.exists()


def test_the_newest_record_of_an_endpoint_can_be_taken_as_of_a_time(journal):
    add(journal, "positions", b"[1]", minutes=0)
    add(journal, "account_summary", b"{}", minutes=1)
    add(journal, "positions", b"[2]", minutes=2)
    assert J.latest("positions", journal).body == b"[2]" and J.latest("positions", journal, T0 + dt.timedelta(minutes=1)).body == b"[1]"
    assert J.latest("dividends", journal) is None


def test_a_record_is_found_by_its_identity_and_an_unknown_one_is_refused(journal):
    rid = add(journal, body=b"[3]")
    assert J.entry(rid, journal).body == b"[3]"
    with pytest.raises(J.JournalError, match="no journal record"):
        J.entry("0" * 64, journal)


def test_the_pages_of_the_newest_pull_come_back_in_order(journal):
    add(journal, "dividends", b'{"p": 0}', pull="dividends@old", page=0, minutes=0)
    add(journal, "dividends", b'{"p": 0}', pull="dividends@new", page=0, minutes=1)
    add(journal, "dividends", b'{"p": 1}', pull="dividends@new", page=1, minutes=2)
    assert [(e.pull, e.page) for e in J.latest_pull("dividends", journal)] == [("dividends@new", 0), ("dividends@new", 1)]


def test_a_pull_with_a_missing_page_is_refused(journal):
    add(journal, "dividends", b"{}", pull="dividends@x", page=0, minutes=0)
    add(journal, "dividends", b"{}", pull="dividends@x", page=2, minutes=1)
    with pytest.raises(J.JournalError, match=r"has pages \[0, 2\]"):
        J.latest_pull("dividends", journal)


def test_a_record_is_written_in_the_exact_format_this_journal_has_always_used(journal):
    rl = RateLimit(limit=1, period_s=5, remaining=0, reset_utc=dt.datetime(2026, 10, 28, 8, 30, 5, tzinfo=dt.UTC), used=1)
    J.append(endpoint="positions", path="/equity/positions", pull="p@1", page=0, status=200, body='[{"q": "é", "x": 1.5}]'.encode(), rate_limit=rl,
             received_utc=dt.datetime(2026, 10, 28, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=1))), journal=journal)
    assert journal.read_bytes() == ('{"body":"[{\\"q\\": \\"é\\", \\"x\\": 1.5}]","body_sha256":"e316aa98e0e239368bf6497a21a9b4c9e1183e4d96799d3e77bb8b86969bf04a",'
                                    '"endpoint":"positions","page":0,"path":"/equity/positions","prev":null,"pull":"p@1","rate_limit":{"limit":1,"period_s":5,'
                                    '"remaining":0,"reset_utc":"2026-10-28T08:30:05+00:00","used":1},"status":200,"utc":"2026-10-28T08:30:00+00:00"}\n').encode()
