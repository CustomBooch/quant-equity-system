"""quant.execution.audit: every record is chained to the line before it, and each way of altering the log is caught. The log is a real file in tmp_path."""
from __future__ import annotations

import json

import pytest

from quant.execution import audit as A


@pytest.fixture(autouse=True)
def log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "PATH", tmp_path / "audit.jsonl")


def three():
    for e in ("plan", "send_started", "sent"):
        A.log(e, plan_hash="h")
    return A.PATH.read_bytes().splitlines()


def rewrite(lines):
    A.PATH.write_bytes(b"\n".join(lines) + b"\n")


def test_each_record_holds_the_hash_of_the_line_before_and_the_first_holds_none():
    ls = three()
    assert [json.loads(x)["prev"] for x in ls] == [None, A.sha(ls[0]), A.sha(ls[1])]
    assert A.verify() == {"records": 3, "unchained": 0, "broken": [], "head": A.sha(ls[2]), "anchor_problem": None}


def test_an_empty_or_absent_log_verifies_with_no_head():
    assert A.verify() == {"records": 0, "unchained": 0, "broken": [], "head": None, "anchor_problem": None}


@pytest.mark.parametrize("alter,broken", [
    (lambda ls: [ls[0], ls[1].replace(b'"h"', b'"x"'), ls[2]], [3]),          # an edited record breaks the record after it
    (lambda ls: [ls[0], ls[2]], [2]),                                         # a deleted record
    (lambda ls: [ls[0], ls[2], ls[1]], [2, 3]),                               # reordered records
    (lambda ls: [ls[0], ls[1], ls[1], ls[2]], [3]),                           # a copied record
    (lambda ls: [ls[0], b"not json", ls[1], ls[2]], [2, 3]),                  # an inserted line that is not a record
    (lambda ls: [ls[0], b'{"event": "sent"}', ls[1], ls[2]], [2, 3]),         # an inserted record without a chain hash
])
def test_any_alteration_except_a_cut_at_the_end_is_reported_by_line(alter, broken):
    rewrite(alter(three()))
    assert A.verify()["broken"] == broken


def test_a_cut_at_the_end_keeps_a_valid_chain_and_shows_only_in_the_head():
    ls = three()
    rewrite(ls[:2])
    assert A.verify() == {"records": 2, "unchained": 0, "broken": [], "head": A.sha(ls[1]), "anchor_problem": None}


def test_records_written_before_the_chain_began_are_counted_and_covered_by_the_first_chained_record(monkeypatch):
    legacy = b'{"utc": "2026-10-24T01:06:55+00:00", "event": "plan", "plan_hash": "h"}'
    monkeypatch.setattr(A, "PRE_CHAIN", frozenset({A.sha(legacy)}))
    rewrite([legacy])
    A.log("refused", plan_hash="h")
    assert A.verify()["unchained"] == 1 and A.verify()["broken"] == []
    ls = A.PATH.read_bytes().splitlines()
    rewrite([legacy.replace(b"plan", b"edit"), ls[1]])
    assert A.verify()["broken"] == [1, 2]                                    # the edited record is no longer a known pre-chain record, and breaks the link after it


def test_a_record_without_a_chain_link_that_is_not_a_known_pre_chain_record_breaks_the_chain():
    three()
    stripped = [json.dumps({k: v for k, v in json.loads(x).items() if k != "prev"}).encode() for x in A.PATH.read_bytes().splitlines()]
    rewrite(stripped)                                                          # every link removed: the whole log would look pre-chain
    assert A.verify()["broken"] == [1, 2, 3]


def test_the_pre_chain_list_holds_only_the_live_logs_one_early_record():
    assert len(A.PRE_CHAIN) == 0


class TestAnchor:
    """Amendment A16: every line git last committed must still be there. The log is a file in a real git repository made in tmp_path."""

    @pytest.fixture()
    def repo(self, tmp_path, monkeypatch):
        import subprocess
        d = tmp_path / "repo"
        d.mkdir()
        monkeypatch.setattr(A, "PATH", d / "audit.jsonl")
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        return git

    def test_a_log_that_only_grew_since_its_commit_is_anchored(self, repo):
        three()
        repo("add", "audit.jsonl")
        repo("commit", "-q", "-m", "x")
        A.log("note")
        assert A.verify()["anchor_problem"] is None

    def test_removed_or_changed_committed_lines_are_reported_even_when_the_chain_is_rebuilt(self, repo):
        three()
        repo("add", "audit.jsonl")
        repo("commit", "-q", "-m", "x")
        kept = A.PATH.read_bytes().splitlines()[:2]
        rewrite(kept)                                                          # the last record cut: the chain is still valid
        assert A.verify()["broken"] == [] and "lines were removed" in A.verify()["anchor_problem"]
        rewrite([json.dumps({**json.loads(kept[0]), "event": "refused", "prev": None}).encode(), *kept[1:], b'{"event": "x", "prev": "' + A.sha(kept[1]).encode() + b'"}'])
        assert "line 1 differs" in A.verify()["anchor_problem"]

    def test_a_log_git_does_not_track_has_no_anchor_and_is_not_failed_for_it(self, repo):
        three()
        assert A.verify()["anchor_problem"] is None
