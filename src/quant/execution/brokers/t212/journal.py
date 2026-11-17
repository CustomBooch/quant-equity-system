"""The Trading 212 read journal, data/raw_ext/t212_journal/journal.jsonl (git-ignored: it holds account data): every successful read response as received, one JSON
line each, in the format of the PAPER audit log and the experiment registry, written and checked by their one shared implementation (quant.core.chain): each
line carries `prev`, the SHA-256 of the line before it, so an edited, inserted, reordered or deleted line breaks the chain; and `body_sha256`, the SHA-256 of
the body as received, so a body changed without its hash shows. The SHA-256 of a line is the record's identity: reports cite it, so each number they show can be traced to the response
it came from. Records are appended and forced to disk before the caller continues; nothing is rewritten. Lines cut from the end leave a valid chain, so a report
records the head it read, against which a later cut shows.

    utc           when the response was received (ISO 8601, UTC)
    endpoint      the read it answered (quant.execution.brokers.t212.client.ENDPOINTS), for example "positions"
    path          the path requested on the practice host, query included
    pull, page    the pages of one paginated read share `pull` and are numbered from 0; a single-response read is page 0 of its own pull
    status        the HTTP status (only successful reads are journaled)
    rate_limit    the response's rate-limit state (quant.execution.brokers.t212.ratelimit.RateLimit.as_json), or null
    body_sha256   SHA-256 of the body bytes
    body          the body as text (UTF-8, as received)
    prev          SHA-256 of the previous line, null on the first
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from quant.core import chain
from quant.core.paths import REPO
from quant.execution.brokers.t212.ratelimit import RateLimit

PATH = REPO / "data/raw_ext/t212_journal/journal.jsonl"
KEYS = frozenset({"utc", "endpoint", "path", "pull", "page", "status", "rate_limit", "body_sha256", "body", "prev"})


class JournalError(Exception):
    """The journal cannot be read as asked: it fails verification, a record is missing, a pull is incomplete, or a body to record is not UTF-8."""


class Verification(TypedDict):
    records: int
    broken: list[int]               # 1-based lines that are not a journal record or whose `prev` is not the hash of the line before
    bad_body: list[int]             # 1-based lines whose body does not hash to their body_sha256
    head: str | None                # SHA-256 of the newest line
    ok: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Entry:
    """One journaled response. `record_sha256` is its identity (the SHA-256 of its line); `line_no` is 1-based."""
    record_sha256: str
    line_no: int
    received_utc: dt.datetime
    endpoint: str
    path: str
    pull: str
    page: int
    status: int
    rate_limit: RateLimit | None
    body: bytes


def sha(data: bytes) -> str:
    return chain.sha(data)


def lines(journal: Path = PATH) -> list[bytes]:
    return chain.lines(journal)


def append(*, endpoint: str, path: str, pull: str, page: int, status: int, body: bytes, rate_limit: RateLimit | None, received_utc: dt.datetime,
           journal: Path = PATH) -> str:
    """Append one response, chained to the line before it, and force it to disk; returns the record's SHA-256. Refuses a body that is not UTF-8 (it could not be
    stored as received) and a naive timestamp."""
    if received_utc.utcoffset() is None:
        raise JournalError("received_utc must be timezone-aware")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise JournalError(f"{endpoint}: body is not UTF-8 ({e.reason} at byte {e.start}); not journaled") from e
    record = {"utc": received_utc.astimezone(dt.UTC).isoformat(), "endpoint": endpoint, "path": path, "pull": pull, "page": page, "status": status,
              "rate_limit": None if rate_limit is None else rate_limit.as_json(), "body_sha256": sha(body), "body": text}
    _, record_sha256 = chain.append(journal, record)                # sorted keys, compact, non-ASCII kept: the bytes this journal has always written
    return record_sha256


def _is_record(rec: dict[str, object]) -> bool:
    """Exactly the documented keys, with the body and its hash as strings."""
    return set(rec) == KEYS and isinstance(rec["body"], str) and isinstance(rec["body_sha256"], str)


def verify(journal: Path = PATH) -> Verification:
    """Check the chain and every body hash. A line is broken if it is not a journal record (exactly the documented keys) or its `prev` is not the hash of the
    line before; a body is bad if it does not hash to its `body_sha256`."""
    ls = lines(journal)
    checked = chain.check(ls, valid=_is_record)
    bad_body: list[int] = []
    for i, line in enumerate(ls):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and _is_record(rec) and sha(rec["body"].encode("utf-8")) != rec["body_sha256"]:
            bad_body.append(i + 1)
    return {"records": checked.records, "broken": list(checked.broken), "bad_body": bad_body, "head": checked.head, "ok": checked.ok and not bad_body}


def entries(journal: Path = PATH) -> list[Entry]:
    """Every record, oldest first. Refuses a journal that fails verification: nothing read from it could be traced."""
    v = verify(journal)
    if not v["ok"]:
        raise JournalError(f"{journal} fails verification: broken line(s) {v['broken']}, bad body line(s) {v['bad_body']}")
    out = []
    for i, line in enumerate(lines(journal)):
        rec = json.loads(line)
        rl = rec["rate_limit"]
        out.append(Entry(record_sha256=sha(line), line_no=i + 1, received_utc=dt.datetime.fromisoformat(rec["utc"]), endpoint=rec["endpoint"], path=rec["path"],
                         pull=rec["pull"], page=rec["page"], status=rec["status"], rate_limit=None if rl is None else RateLimit.from_json(rl),
                         body=rec["body"].encode("utf-8")))
    return out


def entry(record_sha256: str, journal: Path = PATH) -> Entry:
    """The record with this identity."""
    for e in entries(journal):
        if e.record_sha256 == record_sha256:
            return e
    raise JournalError(f"no journal record {record_sha256}")


def latest(endpoint: str, journal: Path = PATH, at_or_before_utc: dt.datetime | None = None) -> Entry | None:
    """The newest record of `endpoint` (received at or before `at_or_before_utc` if given); None if there is none."""
    found = [e for e in entries(journal) if e.endpoint == endpoint and (at_or_before_utc is None or e.received_utc <= at_or_before_utc)]
    return found[-1] if found else None


def latest_pull(endpoint: str, journal: Path = PATH, at_or_before_utc: dt.datetime | None = None) -> tuple[Entry, ...]:
    """Every page of the newest pull of `endpoint` (that pull's last record received at or before `at_or_before_utc` if given), in page order; () if there is
    none. Refuses a pull whose pages are not numbered 0, 1, 2, ... without a gap."""
    newest = latest(endpoint, journal, at_or_before_utc)
    if newest is None:
        return ()
    pages = sorted((e for e in entries(journal) if e.pull == newest.pull and e.endpoint == endpoint), key=lambda e: e.page)
    if [e.page for e in pages] != list(range(len(pages))):
        raise JournalError(f"pull {newest.pull} of {endpoint} has pages {[e.page for e in pages]}, not 0..{len(pages) - 1}")
    return tuple(pages)
