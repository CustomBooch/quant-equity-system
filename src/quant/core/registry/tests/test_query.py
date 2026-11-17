"""quant.core.registry.query: trial counts (also as of a point in time), the failed-experiment table, per-campaign summaries and comparisons."""
from __future__ import annotations

import pytest

from quant.core.registry import query


def r(i, campaign, verdict, trials=1, **metrics):
    return {"id": i, "kind": "candidate", "campaign": campaign, "verdict": verdict, "trials": trials, "source": {"path": "p"}, "metrics": metrics}


RECS = [r("a", "legacy/SB01", "rejected", 3), r("b", "legacy/SB02", "registered", 2), r("c", "bridge", "completed", 720), r("d", "definition", "inconclusive"), r("e", "definition", "placebo", 0), r("f", "definition", "failed", ic=0.04)]


def test_trials_sum_by_campaign_prefix_and_as_of_a_record():
    assert query.trial_count(RECS) == 3 + 2 + 720 + 1 + 0 + 1
    assert query.trial_count(RECS, "legacy") == 5 and query.trial_count(RECS, "definition") == 2
    assert query.trial_count(RECS, through="c") == 5                                # what a result recorded at "c" had to be deflated against: only the records before it


def test_the_failed_table_lists_failures_and_leaves_out_controls_and_unrun_registrations():
    assert [x["id"] for x in query.failed(RECS)] == ["a", "d", "f"] and [x["id"] for x in query.failed(RECS, "definition")] == ["d", "f"]


def test_campaign_summaries_count_records_trials_and_verdicts():
    c = query.campaigns(RECS)
    assert c["definition"] == {"records": 3, "trials": 2, "verdicts": {"inconclusive": 1, "placebo": 1, "failed": 1}} and c["bridge"]["trials"] == 720


def test_a_comparison_gives_the_requested_metrics_in_the_requested_order_and_none_where_missing():
    t = query.compare(RECS, ["f", "a"], ["ic"])
    assert t == [{"id": "f", "verdict": "failed", "ic": 0.04}, {"id": "a", "verdict": "rejected", "ic": None}]
    with pytest.raises(KeyError):
        query.compare(RECS, ["zzz"], ["ic"])
