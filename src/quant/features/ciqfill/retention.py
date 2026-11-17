"""Universe retention between consecutive bridge months.

A bridge month is built from a Capital IQ export matched to permnos, so a stock that is missing from the export (a second share class, a foreign issuer listed in the US, a name the screen
leaves out) silently leaves the universe. The match-rate gates measure the share of export rows that matched a permno, not the share of last month's universe that is still there. This gate
measures the second one: of the previous month's investable stocks, how many have a row this month. Normal attrition (delistings, mergers) is 0.2% to 0.5% a month in the engine months.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MIN_RETENTION = 0.98


def retention(previous_investable: Iterable[int], current_rows: Iterable[int], minimum: float = MIN_RETENTION) -> dict[str, Any]:
    """Share of `previous_investable` that appears in `current_rows`, the lost permnos, and the gate result."""
    prev = {int(p) for p in previous_investable}
    cur = {int(p) for p in current_rows}
    kept = prev & cur
    share = len(kept) / len(prev) if prev else 1.0
    return {"previous_investable": len(prev), "retained": len(kept), "share": round(share, 4), "lost_n": len(prev - cur), "lost_permnos": sorted(prev - cur), "minimum": minimum, "result": "pass" if share >= minimum else "fail"}
