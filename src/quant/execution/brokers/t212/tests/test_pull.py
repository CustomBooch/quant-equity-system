"""quant.execution.brokers.t212.pull: the read command. Boundaries replaced: the broker's HTTP (the opener), the clock and the pause; the journal is a real file."""
from __future__ import annotations

import random

import pytest

from quant.execution.brokers.t212 import client as C
from quant.execution.brokers.t212 import journal as J
from quant.execution.brokers.t212 import pull as P
from quant.execution.brokers.t212.tests import factories as F


@pytest.fixture()
def client(tmp_path, keys, broker, clock):
    return C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(3), journal_path=tmp_path / "journal.jsonl")


def test_by_default_the_account_summary_and_the_positions_are_read_and_journaled(client, broker, tmp_path, capsys):
    broker.answers += [F.ok(F.account_summary()), F.ok([F.position("AAA_US_EQ"), F.position("BBB_US_EQ")])]
    assert P.main([], client=client) == 0
    assert broker.paths() == ["/equity/account/summary", "/equity/positions"]
    out = capsys.readouterr().out.splitlines()
    shas = [e.record_sha256[:12] for e in J.entries(tmp_path / "journal.jsonl")]
    assert out == [f"account_summary: 1 record(s) from 1 response(s); journal {shas[0]}", f"positions: 2 record(s) from 1 response(s); journal {shas[1]}"]


def test_all_runs_every_read_in_order(client, broker):
    broker.answers += [F.ok(F.account_summary()), F.ok([]), F.ok(F.page([])), F.ok(F.page([])), F.ok([]), F.ok([]), F.ok([])]
    assert P.main(["all"], client=client) == 0
    assert broker.paths() == ["/equity/account/summary", "/equity/positions", "/equity/history/dividends?limit=50", "/equity/history/transactions?limit=50",
                              "/equity/history/exports", "/equity/metadata/exchanges", "/equity/metadata/instruments"]


def test_a_failed_read_is_reported_the_others_still_run_and_the_exit_code_says_so(client, broker, capsys):
    broker.answers += [F.status(403, b"Scope( portfolio ) missing for API key"), F.ok(F.account_summary())]
    assert P.main(["positions", "account_summary"], client=client) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("FAILED positions: Forbidden: positions (/equity/positions): HTTP 403") and out[1].startswith("account_summary: 1 record(s)")


def test_without_the_read_keys_nothing_is_requested(client, broker, monkeypatch, capsys):
    monkeypatch.delenv("T212_READ_API_KEY")
    assert P.main([], client=client) == 1
    assert broker.requests == [] and capsys.readouterr().out.startswith("REFUSED: T212_READ_API_KEY or T212_READ_SECRET_KEY is not set")


def test_an_unknown_read_is_refused_by_the_parser(client, broker):
    with pytest.raises(SystemExit) as e:
        P.main(["orders"], client=client)
    assert e.value.code == 2 and broker.requests == []
