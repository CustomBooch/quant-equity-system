"""What each data source may be used for, kept as data the tests can check.

    tools/py -m quant licences           # every source, its recorded limit, and what real-money use would first need

Why: the sources carry different limits (a subscription licence, a vendor's terms, public data), and until now those lived in notes. Here each source is one row. `live_use` is one of
    "yes"          the terms allow use in a real-money pipeline (public data, or the broker's own list for the account that trades)
    "no"           the owner or the terms state paper trading / research only
    "unconfirmed"  no statement of the terms is held; treated as no
Real-money use is blocked by every row that feeds production and is not "yes" (`live_blockers`). Nothing sends a live order today (the engine's host is the
paper account's), so this is the list to clear first, and the tests make sure a new data package or a new production dependency cannot appear without a row:
tools/tests/test_licences.py.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

YES, NO, UNCONFIRMED = "yes", "no", "unconfirmed"


@dataclass(frozen=True)
class Source:
    name: str
    live_use: str
    feeds_production: bool                    # the monthly production path (quant.models.scores, targets, engine) reads data from it
    record: str                               # where the terms or the owner's statement are recorded: a repository path, or the statement itself
    packages: tuple[str, ...]                 # module prefixes whose code reads or pulls the source ("" for data with no code of its own here)
    note: str = ""


SOURCES: tuple[Source, ...] = (                # the sources the public edition has code for; record the terms of each source you add
    Source("Capital IQ", UNCONFIRMED, True, "your own Capital IQ terms", ("quant.data.capiq", "quant.features.ciqfill"), "exports read from files you download"),
    Source("Yahoo Finance (bars, analyst snapshots)", UNCONFIRMED, True, "Yahoo's terms of service", ("quant.data.yahoo", "quant.data.free.archive"), "check the terms before any real-money use"),
    Source("Broker instrument lists (Alpaca, Trading 212)", YES, True, "the broker's own account terms", ("quant.data.instruments",)),
    Source("SEC EDGAR", YES, False, "sec.gov public filings; contact address from the environment only", ("quant.data.sec",)),
    Source("Data source framework", YES, False, "no data of its own: pulls, checks and stores what the plugins fetch", ("quant.data.sources",)),
)


def owner(module: str, sources: tuple[Source, ...] = SOURCES) -> Source | None:
    """The source whose package prefix is the longest one `module` starts with (a module and its submodules), or None."""
    best: tuple[int, Source] | None = None
    for s in sources:
        for p in s.packages:
            if (module == p or module.startswith(p + ".")) and (best is None or len(p) > best[0]):
                best = (len(p), s)
    return best[1] if best else None


def live_blockers(sources: tuple[Source, ...] = SOURCES) -> list[Source]:
    """Sources that feed production and are not cleared for real-money use."""
    return [s for s in sources if s.feeds_production and s.live_use != YES]


def report(sources: tuple[Source, ...] = SOURCES) -> list[str]:
    lines = [f"{s.live_use:<12} {'production' if s.feeds_production else 'research  '}  {s.name}: {s.record}" + (f" ({s.note})" if s.note else "") for s in sources]
    blockers = live_blockers(sources)
    lines.append("real-money use would first need written terms for: " + ("; ".join(s.name for s in blockers) if blockers else "nothing"))
    return lines


def main() -> int:
    print("\n".join(report()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
