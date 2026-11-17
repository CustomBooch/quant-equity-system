"""quant.execution.brokers.t212.health: the read-only layer's health checks. Boundaries replaced: the broker's HTTP (the opener), the clock and pause, and the instrument list's
path; the journal is a real file in tmp_path. Key values are invented and must never appear in any output."""
from __future__ import annotations

import datetime as dt
import json
import random

import pytest

from quant.execution.brokers.t212 import client as C
from quant.execution.brokers.t212 import health as H
from quant.execution.brokers.t212 import journal as J
from quant.data.instruments import t212 as M
from quant.execution.brokers.t212.tests import factories as F

T0 = F.T0
KEY_VALUES = ("k-read", "s-read", "k-full", "s-full")                    # the conftest `keys` fixture's values
HEALTHY_RATE = (1, 5, 0, 5)                                               # the summary's 1 request / 5 s, spent by the check's own read


@pytest.fixture()
def journal(tmp_path):
    return tmp_path / "journal.jsonl"


@pytest.fixture()
def instruments(tmp_path, monkeypatch):
    """The instrument list's folder moved to tmp_path; put(days_old) writes a list pulled that many days before T0."""
    monkeypatch.setattr(M, "instruments_file", lambda day="*": tmp_path / f"t212_{day}" / "instruments.json")
    monkeypatch.setattr(H.preflight, "BROKER", "t212")                         # the Trading 212 layer checks its own list (A10 made preflight follow Alpaca)

    def put(days_old):
        f = M.instruments_file((T0.date() - dt.timedelta(days=days_old)).isoformat())
        f.parent.mkdir(parents=True)
        f.write_text("[]")
    return put


def read(journal, endpoint, path, value, age):
    return J.append(endpoint=endpoint, path=path, pull=f"{endpoint}@{age}", page=0, status=200, body=F.body(value), rate_limit=None, received_utc=T0 - age,
                    journal=journal)


def fresh(journal, age=dt.timedelta(hours=1), positions=None):
    read(journal, "account_summary", "/equity/account/summary", F.account_summary(), age)
    read(journal, "positions", "/equity/positions", [F.position()] if positions is None else positions, age)


def client(journal, clock):
    return C.ReadClient(clock=clock, sleep=clock.sleep, rng=random.Random(1), journal_path=journal)


def health(journal, clock, *args):
    return H.main(["--journal", str(journal), *args], now=T0, client=client(journal, clock))


def lines(capsys):
    return {line.split()[1]: line for line in capsys.readouterr().out.splitlines()[:-1]}


def test_a_healthy_setup_passes_every_check_journals_its_one_read_and_exits_0(keys, broker, clock, journal, instruments, capsys):
    instruments(10)
    fresh(journal)
    broker.answers.append(F.ok(F.account_summary(), rate=HEALTHY_RATE))
    assert health(journal, clock) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == "health: 0 finding(s) in 6 check(s)" and broker.paths() == ["/equity/account/summary"]
    assert [line.split()[:2] for line in out.splitlines()[:-1]] == [["OK", n] for n in ("keys", "journal", "instrument_list", "snapshots", "reachability", "rate_limit")]
    assert "account summary 1.0 h old, positions 1.0 h old (limit 8.0 days)" in out and "0 of 1 request(s) left in the 5 s period after this check's one" in out
    assert f"journal {J.entries(journal)[-1].record_sha256[:12]}" in out and len(J.entries(journal)) == 3 and not any(v in out for v in KEY_VALUES)


def test_key_values_are_never_shown_only_whether_each_is_present_absent_or_still_encrypted(keys, broker, clock, journal, instruments, monkeypatch, capsys):
    monkeypatch.setenv("T212_READ_API_KEY", "encrypted:placeholder-not-a-key")
    monkeypatch.delenv("T212_READ_SECRET_KEY")
    assert health(journal, clock, "--json") == 1
    out = capsys.readouterr().out
    keys_check = json.loads(out)["checks"][0]
    assert keys_check["status"] == "finding" and keys_check["data"]["states"] == {"T212_READ_API_KEY": "still encrypted", "T212_READ_SECRET_KEY": "absent",
                                                                                    "T212_FULL_API_KEY": "present", "T212_FULL_SECRET_KEY": "present"}
    assert "placeholder-not-a-key" not in out and not any(v in out for v in KEY_VALUES) and broker.requests == []


def test_online_without_the_read_keys_no_request_is_made_and_each_network_check_is_a_finding(keys, broker, clock, journal, instruments, monkeypatch, capsys):
    instruments(1)
    fresh(journal)
    monkeypatch.delenv("T212_READ_API_KEY")
    assert health(journal, clock) == 1
    found = lines(capsys)
    assert found["reachability"].startswith("FINDING") and "not attempted: the read keys are not available" in found["reachability"]
    assert found["rate_limit"].startswith("FINDING") and found["keys"].startswith("FINDING") and broker.requests == []


def test_offline_the_keys_are_only_reported_and_the_network_checks_are_skipped(broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal)
    assert health(journal, clock, "--offline") == 0
    found = lines(capsys)
    assert found["keys"].startswith("INFO") and found["keys"].endswith("(not used by this layer); not needed offline")
    assert found["reachability"].split(maxsplit=2)[::2] == ["INFO", "skipped: --offline"] and broker.requests == [] and len(J.entries(journal)) == 2


def test_a_journal_that_fails_verification_is_a_finding_and_nothing_is_read_from_it(broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal)
    first, second = journal.read_bytes().splitlines()
    journal.write_bytes(first.replace(b'"page":0', b'"page":1') + b"\n" + second + b"\n")
    assert health(journal, clock, "--offline") == 1
    found = lines(capsys)
    assert "fails verification: broken line(s) [2], bad body line(s) []" in found["journal"] and found["journal"].startswith("FINDING")
    assert "not measured:" in found["snapshots"] and "newest journaled read: unknown: the journal fails verification" in found["instrument_list"]


def test_an_empty_journal_is_not_a_finding_but_the_missing_snapshots_are(broker, clock, journal, instruments, capsys):
    instruments(1)
    assert health(journal, clock, "--offline") == 1
    found = lines(capsys)
    assert found["journal"].startswith("OK") and "no journal yet at" in found["journal"]
    assert found["snapshots"].startswith("FINDING") and "no account summary read in the journal; no positions read in the journal" in found["snapshots"]


@pytest.mark.parametrize("days_old,status", [(31, "OK"), (32, "FINDING")])
def test_an_instrument_list_more_than_31_days_old_is_a_finding(broker, clock, journal, instruments, capsys, days_old, status):
    instruments(days_old)
    fresh(journal)
    read(journal, "instruments", "/equity/metadata/instruments", [F.tradable_instrument()], dt.timedelta(days=2))
    health(journal, clock, "--offline")
    found = lines(capsys)["instrument_list"]
    assert found.startswith(status) and f"instrument list dated {(T0.date() - dt.timedelta(days=days_old)).isoformat()}" in found
    assert f"newest journaled read: 2.0 days old (journal {J.entries(journal)[-1].record_sha256[:12]})" in found


def test_no_instrument_list_is_a_finding_that_names_the_command_to_run(broker, clock, journal, instruments, capsys):
    fresh(journal)
    assert health(journal, clock, "--offline") == 1
    assert "no instrument list; run python -m quant.execution.brokers.t212.pull_instruments" in lines(capsys)["instrument_list"]


def test_a_snapshot_older_than_the_limit_is_a_finding_and_the_limit_can_be_set(broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal, age=dt.timedelta(days=9))
    assert health(journal, clock, "--offline") == 1
    assert "the newest account summary read is 9.0 days old; the newest positions read is 9.0 days old" in lines(capsys)["snapshots"]
    assert health(journal, clock, "--offline", "--max-snapshot-age-days", "10") == 0


def test_a_journaled_snapshot_that_no_longer_parses_is_a_finding(broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal, positions=[{**F.position(), "quantity": "1"}])
    assert health(journal, clock, "--offline") == 1
    found = lines(capsys)["snapshots"]
    assert found.startswith("FINDING") and "not measured: journal record" in found and "quantity: expected number" in found


def test_freshness_is_measured_before_the_command_s_own_read(keys, broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal, age=dt.timedelta(days=9))
    broker.answers.append(F.ok(F.account_summary(), rate=HEALTHY_RATE))
    assert health(journal, clock) == 1
    assert "the newest account summary read is 9.0 days old" in lines(capsys)["snapshots"] and J.entries(journal)[-1].endpoint == "account_summary"


@pytest.mark.parametrize("answer,error", [(F.status(401, b'{"code": "unauthorized"}'), "Unauthorized: "), (OSError("network is down"), "TransportError: ")])
def test_an_unreachable_api_is_a_finding_that_names_the_error(keys, broker, clock, journal, instruments, capsys, answer, error):
    instruments(1)
    fresh(journal)
    broker.answers.append(answer)
    assert health(journal, clock) == 1
    found = lines(capsys)
    assert found["reachability"].split(maxsplit=2)[2].startswith(error) and "not measured: the read failed" in found["rate_limit"]


@pytest.mark.parametrize("answers,problem", [
    ([F.ok(F.account_summary())], "the response carried no rate-limit headers: the client cannot pace by them"),
    ([F.ok(F.account_summary(), rate=(5, 5, 2, 5))], "other requests share the key's budget; 2 of 5 request(s) left"),
    ([F.ok(F.account_summary(), rate=(1, 5, 0, 60))], "the reset is 60 s away, beyond the 5 s period"),
    ([F.status(429, rate=(1, 5, 0, 2)), F.ok(F.account_summary(), rate=HEALTHY_RATE)], "HTTP 429 met 1 time(s) before the read succeeded"),
])
def test_rate_limit_headroom_that_is_missing_shared_inconsistent_or_spent_is_a_finding(keys, broker, clock, journal, instruments, capsys, answers, problem):
    instruments(1)
    fresh(journal)
    broker.answers += answers
    assert health(journal, clock) == 1
    found = lines(capsys)
    assert found["rate_limit"].startswith("FINDING") and problem in found["rate_limit"] and found["reachability"].startswith("OK")


def test_a_retried_read_reports_its_retries(keys, broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal)
    broker.answers += [F.status(503), F.ok(F.account_summary(), rate=HEALTHY_RATE)]
    assert health(journal, clock) == 0
    assert "HTTP 200 after 1 retry, journal" in lines(capsys)["reachability"]


def test_the_json_output_is_deterministic_and_carries_every_check_in_order(broker, clock, journal, instruments, capsys):
    instruments(1)
    fresh(journal)
    health(journal, clock, "--offline", "--json")
    first = capsys.readouterr().out
    health(journal, clock, "--offline", "--json")
    doc = json.loads(first)
    assert capsys.readouterr().out == first and (doc["kind"], doc["mode"], doc["findings"], doc["generated_utc"]) == ("t212_health", "offline", 0, T0.isoformat())
    assert [c["name"] for c in doc["checks"]] == ["keys", "journal", "instrument_list", "snapshots", "reachability", "rate_limit"]
