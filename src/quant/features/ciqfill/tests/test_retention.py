"""quant.features.ciqfill.retention: the universe-retention gate."""
from __future__ import annotations

from quant.features.ciqfill.retention import retention


def test_retention_gate_fails_when_a_screen_silently_drops_stocks_and_passes_normal_attrition():

    prev = set(range(1000))
    ok = retention(prev, set(range(1000)) - {1, 2, 3, 4, 5})           # 0.5% attrition
    assert ok["result"] == "pass" and ok["lost_n"] == 5 and ok["share"] == 0.995
    bad = retention(prev, set(range(1000)) - set(range(64)))           # 6.4% lost, as at the July to August splice
    assert bad["result"] == "fail" and bad["lost_permnos"] == list(range(64)) and bad["share"] == 0.936
    new_only = retention(prev, set(range(1000)) | {5000, 5001})        # newcomers never lower retention
    assert new_only["result"] == "pass" and new_only["share"] == 1.0
    assert retention(set(), {1, 2})["result"] == "pass"                # nothing to retain
