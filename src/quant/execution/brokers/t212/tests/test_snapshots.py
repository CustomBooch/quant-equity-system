"""quant.execution.brokers.t212.snapshots: typed views of journaled responses. A real journal in tmp_path, written by quant.execution.brokers.t212.journal.append."""
from __future__ import annotations

import datetime as dt

import pytest

from quant.execution.brokers.t212 import journal as J
from quant.execution.brokers.t212 import snapshots as SN
from quant.execution.brokers.t212.tests import factories as F

T0 = F.T0


def put(path, endpoint, read_path, value, minutes):
    return J.append(endpoint=endpoint, path=read_path, pull=f"{endpoint}@{minutes}", page=0, status=200, body=F.body(value), rate_limit=None,
                    received_utc=T0 + dt.timedelta(minutes=minutes), journal=path)


@pytest.fixture()
def journal(tmp_path):
    p = tmp_path / "journal.jsonl"
    put(p, "positions", "/equity/positions", [F.position("AAA_US_EQ", 1.0)], 0)
    put(p, "account_summary", "/equity/account/summary", F.account_summary(available=100.0), 1)
    put(p, "positions", "/equity/positions?ticker=BBB_US_EQ", [F.position("BBB_US_EQ", 9.0)], 2)      # one ticker only: not an account snapshot
    put(p, "positions", "/equity/positions", [F.position("AAA_US_EQ", 3.0)], 3)
    return p


def test_the_newest_full_positions_read_is_taken_and_a_filtered_one_is_not(journal):
    s = SN.latest_positions(journal)
    assert [p.quantity_shares for p in s.data] == [3.0] and s.received_utc == T0 + dt.timedelta(minutes=3)
    assert s.record_sha256 == J.entries(journal)[3].record_sha256 and len(SN.position_reads(journal)) == 2


def test_positions_and_summaries_can_be_taken_as_of_a_time(journal):
    assert [p.quantity_shares for p in SN.latest_positions(journal, T0 + dt.timedelta(minutes=2)).data] == [1.0]
    assert SN.latest_summary(journal).data.cash.available_to_trade_account_ccy == 100.0 and SN.latest_summary(journal, T0) is None


def test_the_first_positions_read_after_a_time_is_found(journal):
    assert [p.quantity_shares for p in SN.first_positions_after(T0, journal).data] == [3.0]
    assert SN.first_positions_after(T0 + dt.timedelta(minutes=3), journal) is None


def test_a_positions_read_is_found_by_its_record_and_another_read_is_refused(journal):
    entries = J.entries(journal)
    assert [p.quantity_shares for p in SN.positions_record(entries[0].record_sha256, journal).data] == [1.0]
    with pytest.raises(SN.SnapshotError, match="is a positions read of /equity/positions\\?ticker=BBB_US_EQ, not a full positions read"):
        SN.positions_record(entries[2].record_sha256, journal)


def test_a_journaled_body_that_no_longer_parses_names_its_record(tmp_path):
    p = tmp_path / "j.jsonl"
    rid = put(p, "positions", "/equity/positions", [{**F.position(), "quantity": "1"}], 0)
    with pytest.raises(SN.SnapshotError, match=f"journal record {rid} .*quantity: expected number"):
        SN.latest_positions(p)


def test_an_empty_journal_has_no_snapshot(tmp_path):
    assert SN.latest_positions(tmp_path / "none.jsonl") is None and SN.latest_summary(tmp_path / "none.jsonl") is None


def test_every_full_positions_read_is_parsed_oldest_first(journal):
    assert [[p.quantity_shares for p in s.data] for s in SN.all_positions(journal)] == [[1.0], [3.0]] and SN.all_positions(journal.parent / "none") == []


def test_the_pages_of_the_newest_order_history_pull_are_parsed_in_page_order(tmp_path):
    p = tmp_path / "j.jsonl"
    older = F.page([F.historical_order(1)], "/api/v0/next")
    for pull, page, value in (("a", 0, older), ("b", 0, F.page([F.historical_order(2)], "/api/v0/next")), ("b", 1, F.page([F.historical_order(3)]))):
        J.append(endpoint=SN.HISTORY_ENDPOINT, path="/history", pull=pull, page=page, status=200, body=F.body(value), rate_limit=None, received_utc=T0, journal=p)
    pages = SN.history_pages(p)
    assert [[h.order.order_id for h in s.data] for s in pages] == [[2], [3]] and pages[1].record_sha256 == J.entries(p)[2].record_sha256
    assert SN.history_pages(tmp_path / "none") == []
