"""quant.core.chain: append-only hash-chained JSON lines. Real files in tmp_path. The frozen PAPER audit log and experiment registry are the reference: the
helper must write the same bytes they write and find the same breaks they find."""
from __future__ import annotations

import json

import pytest

from quant.execution import audit as A
from quant.core.registry import store as RS
from quant.core import chain as CH


def three(path, dumps=CH.compact):
    return [CH.append(path, {"n": n, "text": "é"}, dumps)[1] for n in range(3)]


def test_each_record_is_one_line_chained_to_the_line_before_and_identified_by_the_hash_of_its_line(tmp_path):
    ids = three(tmp_path / "c.jsonl")
    ls = CH.lines(tmp_path / "c.jsonl")
    assert [json.loads(x)["prev"] for x in ls] == [None, CH.sha(ls[0]), CH.sha(ls[1])] and ids == [CH.sha(x) for x in ls]
    assert ls[0] == '{"n":0,"prev":null,"text":"é"}'.encode() and CH.check(ls).ok and CH.check(ls).head == ids[-1]


def test_append_returns_the_record_as_written_with_prev_after_its_own_fields(tmp_path):
    full, _ = CH.append(tmp_path / "c.jsonl", {"b": 1, "a": 2})
    assert list(full) == ["b", "a", "prev"] and full["prev"] is None


@pytest.mark.parametrize("tamper,broken", [
    (lambda ls: [ls[0], ls[2]], (2,)),                                                  # deleted
    (lambda ls: [ls[1], ls[0], ls[2]], (1, 2, 3)),                                      # reordered
    (lambda ls: [ls[0], ls[1].replace(b'"n":1', b'"n":9'), ls[2]], (3,)),               # edited: the next line no longer chains
    (lambda ls: [ls[0], ls[1].replace(b'"n":1', b'"n":5'), ls[1], ls[2]], (3,)),        # inserted, chained to the line before
    (lambda ls: [ls[0], b"not json", ls[2]], (2, 3)),
    (lambda ls: [ls[0], b"[1, 2]", ls[2]], (2, 3)),                                     # JSON, but not an object
])
def test_every_kind_of_tampering_is_reported_by_line(tmp_path, tamper, broken):
    three(tmp_path / "c.jsonl")
    assert CH.check(tamper(CH.lines(tmp_path / "c.jsonl"))).broken == broken


def test_a_record_the_caller_rejects_is_broken(tmp_path):
    three(tmp_path / "c.jsonl")
    assert CH.check(CH.lines(tmp_path / "c.jsonl"), valid=lambda r: r["n"] != 1).broken == (2,)


def test_leading_unchained_records_are_counted_only_when_allowed_and_one_after_the_chain_began_is_broken(tmp_path):
    ls = [b'{"a":1}', b'{"a":2}']
    ls.append(json.dumps({"a": 3, "prev": CH.sha(ls[1])}).encode())
    ls.append(b'{"a":4}')
    allowed = CH.check(ls, unchained_prefix=True)
    assert (allowed.unchained, allowed.broken) == (2, (4,)) and CH.check(ls).broken == (2, 4)


def test_an_empty_or_absent_file_has_no_records_and_no_head(tmp_path):
    c = CH.check(CH.lines(tmp_path / "none.jsonl"))
    assert (c.records, c.broken, c.unchained, c.head, c.ok) == (0, (), 0, None, True)


def test_a_record_with_prev_or_a_multi_line_serialisation_is_refused_and_nothing_is_written(tmp_path):
    with pytest.raises(CH.ChainError, match="already has `prev`"):
        CH.append(tmp_path / "c.jsonl", {"prev": "x"})
    with pytest.raises(CH.ChainError, match="not a single line"):
        CH.append(tmp_path / "c.jsonl", {"a": 1}, lambda r: json.dumps(r, indent=1))
    assert not (tmp_path / "c.jsonl").exists()


def test_the_bytes_are_those_the_frozen_audit_log_writes_with_its_serialisation(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "PATH", tmp_path / "audit.jsonl")
    rec = A.log("plan", plan_hash="h", orders=2)
    CH.append(tmp_path / "chain.jsonl", {k: v for k, v in rec.items() if k != "prev"}, lambda r: json.dumps(r, default=str))
    assert (tmp_path / "chain.jsonl").read_bytes() == (tmp_path / "audit.jsonl").read_bytes()
    assert CH.check(CH.lines(tmp_path / "audit.jsonl"), unchained_prefix=True).broken == tuple(A.verify()["broken"])


def test_the_bytes_are_those_the_frozen_registry_writes_with_its_serialisation(tmp_path):
    full = {"id": "x", "recorded_utc": "2026-10-28T00:00:00+00:00", "content_sha": "c"}
    CH.append(tmp_path / "a.jsonl", full, lambda r: json.dumps(r, sort_keys=True, separators=(",", ":")))
    line = CH.lines(tmp_path / "a.jsonl")[0]
    assert line == json.dumps({**full, "prev": None}, sort_keys=True, separators=(",", ":")).encode() and CH.check([line]).broken == ()
    assert RS.sha(line) == CH.sha(line)
