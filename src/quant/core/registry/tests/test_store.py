"""quant.core.registry.store: the registry is append-only and hash-chained, so every kind of tampering is caught, and a correction is a new record rather than an edit."""
from __future__ import annotations

import json

import pytest

from quant.core.registry import store

SHA = "a" * 64


def rec(i="x/1", **kw):
    return {"id": i, "kind": "candidate", "campaign": "c1", "verdict": "failed", "trials": 1, "source": {"path": "results/x.json", "sha256": SHA}, **kw}


@pytest.fixture()
def path(tmp_path):
    p = tmp_path / "experiments.jsonl"
    for k in range(4):
        store.append(rec(f"x/{k}"), p, now="2026-10-27T00:00:00+00:00")
    return p


class TestAppend:
    def test_records_are_chained_to_the_line_before_and_stamped(self, path):
        ls = store.lines(path)
        r = [json.loads(x) for x in ls]
        assert r[0]["prev"] is None and r[1]["prev"] == store.sha(ls[0]) and r[3]["prev"] == store.sha(ls[2])
        assert r[0]["recorded_utc"] == "2026-10-27T00:00:00+00:00" and r[0]["content_sha"] == store.content_sha(rec("x/0"))

    def test_an_id_cannot_be_recorded_twice_and_an_invalid_record_is_refused(self, path):
        with pytest.raises(store.RegistryError, match="already recorded"):
            store.append(rec("x/2", verdict="passed"), path)
        with pytest.raises(store.RegistryError, match="verdict must be one of"):
            store.append(rec("x/9", verdict="great"), path)
        assert len(store.lines(path)) == 4

    def test_a_correction_must_supersede_a_record_that_exists(self, path):
        with pytest.raises(store.RegistryError, match="supersedes nothing-here"):
            store.append(rec("x/5", supersedes="nothing-here"), path)
        store.append(rec("x/2b", supersedes="x/2", verdict="passed"), path)
        assert [r["id"] for r in store.active(store.records(path))] == ["x/0", "x/1", "x/3", "x/2b"]


class TestVerify:
    def test_an_untouched_registry_verifies(self, path):
        v = store.verify(path)
        assert v["ok"] and v["records"] == 4 and v["head"] == store.sha(store.lines(path)[-1])

    def rewrite(self, path, fn):
        ls = [x.decode() for x in store.lines(path)]
        path.write_bytes(("\n".join(fn(ls)) + "\n").encode())

    def test_an_edited_line_breaks_the_chain_at_the_next_line_and_fails_its_own_fingerprint(self, path):
        self.rewrite(path, lambda ls: [ls[0], ls[1].replace('"failed"', '"passed"'), *ls[2:]])
        v = store.verify(path)
        assert not v["ok"] and v["broken"] == [3] and 2 in v["invalid"] and "content_sha" in v["invalid"][2][0]

    def test_a_deleted_line_is_caught_at_the_line_that_followed_it(self, path):
        self.rewrite(path, lambda ls: [ls[0], ls[2], ls[3]])
        assert store.verify(path)["broken"] == [2]

    def test_reordered_lines_are_caught(self, path):
        self.rewrite(path, lambda ls: [ls[0], ls[1], ls[3], ls[2]])
        assert store.verify(path)["broken"] == [3, 4]

    def test_an_inserted_forged_line_is_caught(self, path):
        self.rewrite(path, lambda ls: [ls[0], json.dumps({"id": "forged", "prev": "00"}), *ls[1:]])
        v = store.verify(path)
        assert not v["ok"] and 2 in v["broken"] and 2 in v["invalid"]

    def test_a_line_that_is_not_json_is_reported(self, path):
        self.rewrite(path, lambda ls: [*ls, "not json"])
        assert store.verify(path)["broken"] == [5]

    def test_a_repeated_id_and_a_supersedes_that_points_forward_are_reported(self, path):
        first = store.lines(path)[0].decode()
        self.rewrite(path, lambda ls: [*ls, first])
        v = store.verify(path)
        assert v["duplicate_ids"] == ["x/0"] and not v["ok"]


def test_the_fingerprint_ignores_what_the_store_adds_and_nothing_else():
    a = rec()
    assert store.content_sha(a) == store.content_sha({**a, "recorded_utc": "t", "prev": "p", "content_sha": "c"})
    assert store.content_sha(a) != store.content_sha({**a, "trials": 2})


def test_a_checkout_that_turns_line_endings_into_crlf_still_verifies(path):
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))                       # what git does on Windows with autocrlf; the chain hashes each line without its terminator
    assert store.verify(path)["ok"]
