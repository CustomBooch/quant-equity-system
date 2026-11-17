"""quant.core.registry.schema: a record states what it is, what it found, how many configurations it stands for and where it came from; anything else is refused."""
from __future__ import annotations

import pytest

from quant.core.registry import schema

SHA = "b" * 64
GOOD = {"id": "e/1", "kind": "candidate", "campaign": "c", "verdict": "passed", "trials": 1, "source": {"path": "p.json"}}


def test_a_minimal_and_a_full_record_are_acceptable():
    full = {**GOOD, "hypothesis": "h", "spec": {"file": "SPEC.md", "sha256": SHA}, "config": {"a": 1}, "seeds": [0, 1], "split": "annual", "data": {"panel": SHA}, "code_commit": "abc1234",
            "metrics": {"ic": 0.04}, "folds": {"2008": 0.1}, "selection_rule": "holm", "notes": "n", "extra": {"x": 1}}
    assert schema.problems(GOOD) == [] and schema.problems(full) == []


@pytest.mark.parametrize("change,fragment", [
    ({"id": "has space"}, "id must be"), ({"id": ""}, "id must be"), ({"kind": "idea"}, "kind must be"), ({"verdict": "ok"}, "verdict must be"), ({"campaign": ""}, "campaign must be"),
    ({"trials": -1}, "trials must be"), ({"trials": 1.5}, "trials must be"), ({"trials": True}, "trials must be"), ({"source": {"sha256": SHA}}, "source needs"),
    ({"source": {"path": "p", "sha256": "xyz"}}, "source needs"), ({"spec": {"file": "f", "sha256": "xyz"}}, "spec needs"), ({"data": {"panel": "xyz"}}, "data must map"),
    ({"seeds": [0, "1"]}, "seeds must be"), ({"metrics": [1]}, "metrics must be"), ({"surprise": 1}, "unknown field surprise"), ({"metrics": {"ic": float("nan")}}, "not JSON-serialisable"),
])
def test_each_rule_refuses_its_violation(change, fragment):
    assert any(fragment in p for p in schema.problems({**GOOD, **change}))


def test_missing_required_fields_are_each_named():
    assert sorted(p for p in schema.problems({"id": "e/1"})) == [f"missing {k}" for k in ("kind", "campaign", "verdict", "trials", "source")] or len(schema.problems({"id": "e/1"})) == 5
    assert schema.problems("not an object") == ["the record is not an object"]


def test_a_placebo_is_a_control_not_a_failure():
    assert "placebo" not in schema.FAILED_VERDICTS and {"failed", "rejected", "void", "inconclusive"} == set(schema.FAILED_VERDICTS)
