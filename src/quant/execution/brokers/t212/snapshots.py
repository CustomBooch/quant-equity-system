"""Typed views of journaled responses (quant.execution.brokers.t212.journal): the account summary or the positions as of a time, every positions read, and the pages of the newest
order-history read, parsed by quant.execution.brokers.t212.records, with the journal record each came from. Offline: nothing is requested. A positions snapshot is a full read of
the account's positions; a read filtered to one ticker is not one."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from quant.execution.brokers.t212 import journal
from quant.execution.brokers.t212.client import ACCOUNT_SUMMARY, POSITIONS
from quant.execution.brokers.t212.records import AccountSummary, HistoricalOrder, Position, parse_account_summary, parse_historical_orders_page, parse_positions
from quant.execution.brokers.t212.schema import SchemaError, loads

T = TypeVar("T")
HISTORY_ENDPOINT = "historical_orders"                      # the journal name for the order-history read; this layer does not read it yet (INTEGRATION_SPEC.md)


class SnapshotError(Exception):
    """A journaled body that no longer parses as its read's documented schema."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot(Generic[T]):
    """Typed records read from one journaled response, with its identity and time."""
    data: T
    record_sha256: str
    received_utc: dt.datetime


def typed(e: journal.Entry, parse: Callable[[object], T]) -> Snapshot[T]:
    """The journaled body of `e` parsed by `parse`; SnapshotError naming the record if it does not parse."""
    try:
        return Snapshot(data=parse(loads(e.body)), record_sha256=e.record_sha256, received_utc=e.received_utc)
    except SchemaError as err:
        raise SnapshotError(f"journal record {e.record_sha256} ({e.endpoint}, line {e.line_no}): {err}") from err


def _full(e: journal.Entry, endpoint: str, path: str) -> bool:
    return e.endpoint == endpoint and e.path == path


def summaries(journal_path: Path = journal.PATH) -> list[journal.Entry]:
    return [e for e in journal.entries(journal_path) if _full(e, ACCOUNT_SUMMARY.name, ACCOUNT_SUMMARY.path)]


def position_reads(journal_path: Path = journal.PATH) -> list[journal.Entry]:
    """Every full positions read in the journal, oldest first."""
    return [e for e in journal.entries(journal_path) if _full(e, POSITIONS.name, POSITIONS.path)]


def latest_summary(journal_path: Path = journal.PATH, at_or_before_utc: dt.datetime | None = None) -> Snapshot[AccountSummary] | None:
    found = [e for e in summaries(journal_path) if at_or_before_utc is None or e.received_utc <= at_or_before_utc]
    return typed(found[-1], parse_account_summary) if found else None


def latest_positions(journal_path: Path = journal.PATH, at_or_before_utc: dt.datetime | None = None) -> Snapshot[tuple[Position, ...]] | None:
    found = [e for e in position_reads(journal_path) if at_or_before_utc is None or e.received_utc <= at_or_before_utc]
    return typed(found[-1], parse_positions) if found else None


def first_positions_after(after_utc: dt.datetime, journal_path: Path = journal.PATH) -> Snapshot[tuple[Position, ...]] | None:
    """The oldest full positions read received after `after_utc`."""
    found = [e for e in position_reads(journal_path) if e.received_utc > after_utc]
    return typed(found[0], parse_positions) if found else None


def positions_record(record_sha256: str, journal_path: Path = journal.PATH) -> Snapshot[tuple[Position, ...]]:
    """The full positions read with this journal identity; SnapshotError if the record is another read."""
    e = journal.entry(record_sha256, journal_path)
    if not _full(e, POSITIONS.name, POSITIONS.path):
        raise SnapshotError(f"journal record {record_sha256} is a {e.endpoint} read of {e.path}, not a full positions read")
    return typed(e, parse_positions)


def all_positions(journal_path: Path = journal.PATH) -> list[Snapshot[tuple[Position, ...]]]:
    """Every full positions read in the journal, oldest first, parsed."""
    return [typed(e, parse_positions) for e in position_reads(journal_path)]


def history_pages(journal_path: Path = journal.PATH) -> list[Snapshot[tuple[HistoricalOrder, ...]]]:
    """The items of each page of the newest order-history pull in the journal, in page order; [] if there is none (quant.execution.brokers.t212.journal.latest_pull refuses a pull
    with a missing page)."""
    return [typed(e, lambda v: parse_historical_orders_page(v).items) for e in journal.latest_pull(HISTORY_ENDPOINT, journal_path)]
