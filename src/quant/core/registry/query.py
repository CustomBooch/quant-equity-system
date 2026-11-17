"""Questions put to the registry: how many configurations have been tried, what failed, how candidates compare. All take the active records (`store.active`)."""
from __future__ import annotations

from collections import Counter
from typing import Any

from quant.core.registry import schema
from quant.core.registry.schema import Record


def trial_count(recs: list[Record], campaign: str | None = None, through: str | None = None) -> int:
    """Configurations tried: the sum of `trials`, optionally for campaigns starting with `campaign`, and optionally only for records recorded before the record `through` (the search-space
    size a result had to be deflated against when it was produced)."""
    n = 0
    for r in recs:
        if through is not None and r["id"] == through:
            break
        if campaign is None or r["campaign"].startswith(campaign):
            n += r["trials"]
    return n


def campaigns(recs: list[Record]) -> dict[str, dict[str, Any]]:
    """Per campaign: records, trials and the count of each verdict."""
    out: dict[str, dict[str, Any]] = {}
    for r in recs:
        c = out.setdefault(r["campaign"], {"records": 0, "trials": 0, "verdicts": Counter()})
        c["records"] += 1
        c["trials"] += r["trials"]
        c["verdicts"][r["verdict"]] += 1
    return {k: {**v, "verdicts": dict(v["verdicts"])} for k, v in out.items()}


def failed(recs: list[Record], campaign: str | None = None) -> list[Record]:
    """The failed-experiment table: records whose verdict is failed, rejected, void or inconclusive."""
    return [r for r in recs if r["verdict"] in schema.FAILED_VERDICTS and (campaign is None or r["campaign"].startswith(campaign))]


def compare(recs: list[Record], ids: list[str], metrics: list[str]) -> list[dict[str, Any]]:
    """Rows (id, verdict, then the requested metrics; None where a record lacks one) for the named records, in the order asked; an unknown id raises KeyError."""
    by = {r["id"]: r for r in recs}
    return [{"id": i, "verdict": by[i]["verdict"], **{m: by[i].get("metrics", {}).get(m) for m in metrics}} for i in ids]
