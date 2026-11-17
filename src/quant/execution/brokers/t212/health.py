"""Health of the Trading 212 read-only layer (lab/t212/INTEGRATION_SPEC.md, "Health"): one line per check; exit code 1 on any finding, else 0. The only request
is one GET of the account summary, journaled; a key's value is never shown.

    dotenvx run -- tools/py -m quant.execution.brokers.t212.health          # every check
    tools/py -m quant.execution.brokers.t212.health --offline               # no network: keys (reported only), journal, instrument list, snapshot freshness
    options: --json (the result as JSON), --journal PATH, --max-snapshot-age-days N (default 8: the engine's cadence is weekly)

Checks, in order:
  keys             the read keys T212_READ_API_KEY and T212_READ_SECRET_KEY: present, absent or still encrypted (never a value); a finding unless both are
                   present (offline: reported only). The order keys T212_FULL_* are reported; this layer never uses them.
  journal          the read journal's hash chain and body hashes (quant.execution.brokers.t212.journal.verify); an empty journal is not a finding
  instrument_list  quant.execution.preflight's rule: an instrument list from quant.execution.brokers.t212.pull_instruments at most 31 days old (the newest journaled instruments
                   read is reported beside it: preflight does not read the journal)
  snapshots        the newest account summary and full positions read in the journal: present, parsing as documented, each at most the maximum age old,
                   measured before this command's own read
  reachability     the GET of the account summary through quant.execution.brokers.t212.client (timeouts, retries on 429 and 5xx, strict validation), journaled
  rate_limit       that response's rate-limit headers: present (malformed ones fail the read), no HTTP 429 met, the reset within the period, and all of the
                   period's budget but this check's one request left (less means another client is using the key)
"""
from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quant.execution import preflight
from quant.execution.brokers.t212 import journal, reports, snapshots
from quant.core.env import env
from quant.execution.brokers.t212.client import ACCOUNT_SUMMARY, INSTRUMENTS, ReadClient, ReadError, Wait, live_client
from quant.execution.brokers.t212.ratelimit import RateLimit

READ_KEYS = ("T212_READ_API_KEY", "T212_READ_SECRET_KEY")
ORDER_KEYS = ("T212_FULL_API_KEY", "T212_FULL_SECRET_KEY")
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 8.0
RESET_SLACK_S = 1.0                                         # the reset header is in whole seconds


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    """One check's outcome: `status` ok, finding, or info (reported, not judged: offline, or a check that does not apply)."""
    name: str
    status: str
    detail: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "data": dict(self.data)}


def _age(td: dt.timedelta) -> str:
    return f"{td.total_seconds() / 86400:.1f} days" if td >= dt.timedelta(days=2) else f"{td.total_seconds() / 3600:.1f} h"


def key_state(name: str) -> str:
    """present, absent or still encrypted (quant.execution.brokers.t212.api.env's rule). The value itself is never returned."""
    try:
        return "present" if env(name) else "absent"
    except RuntimeError:
        return "still encrypted"


def check_keys(online: bool) -> Check:
    states = {k: key_state(k) for k in (*READ_KEYS, *ORDER_KEYS)}
    ready = all(states[k] == "present" for k in READ_KEYS)
    detail = (", ".join(f"{k} {states[k]}" for k in READ_KEYS) + " (values never shown); order keys " + ", ".join(f"{k} {states[k]}" for k in ORDER_KEYS)
              + " (not used by this layer)" + ("" if ready or online else "; not needed offline"))
    return Check(name="keys", status="ok" if ready else "finding" if online else "info", detail=detail, data={"states": states, "read_keys_ready": ready})


def check_journal(journal_path: Path) -> Check:
    v = journal.verify(journal_path)
    data = {"records": v["records"], "head": v["head"], "broken": v["broken"], "bad_body": v["bad_body"]}
    if not v["records"]:
        return Check(name="journal", status="ok", detail=f"no journal yet at {journal_path}", data=data)
    if not v["ok"]:
        return Check(name="journal", status="finding", detail=f"{journal_path} fails verification: broken line(s) {v['broken']}, bad body line(s) {v['bad_body']}",
                     data=data)
    return Check(name="journal", status="ok", detail=f"{v['records']} records, chain and body hashes intact (head {str(v['head'])[:12]})", data=data)


def check_instrument_list(now: dt.datetime, journal_path: Path) -> Check:
    ok, detail = preflight.instrument_list_age(now.astimezone(dt.UTC).date())
    try:
        newest = journal.latest(INSTRUMENTS.name, journal_path)
        journaled = "none" if newest is None else f"{_age(now - newest.received_utc)} old (journal {newest.record_sha256[:12]})"
    except journal.JournalError:
        newest, journaled = None, "unknown: the journal fails verification"
    return Check(name="instrument_list", status="ok" if ok else "finding", detail=f"{detail} (quant.execution.preflight: at most 31 days); newest journaled read: {journaled}",
                 data={"preflight_ok": ok, "journaled_record": None if newest is None else newest.record_sha256,
                       "journaled_utc": None if newest is None else newest.received_utc.isoformat()})


def check_snapshots(now: dt.datetime, journal_path: Path, max_age: dt.timedelta) -> Check:
    try:
        reads = (("account summary", snapshots.latest_summary(journal_path)), ("positions", snapshots.latest_positions(journal_path)))
    except (journal.JournalError, snapshots.SnapshotError) as e:
        return Check(name="snapshots", status="finding", detail=f"not measured: {e}")
    parts: list[str] = []
    problems: list[str] = []
    data: dict[str, dict[str, str] | None] = {}
    for label, snap in reads:
        if snap is None:
            problems.append(f"no {label} read in the journal")
            data[label.replace(" ", "_")] = None
            continue
        age = now - snap.received_utc
        parts.append(f"{label} {_age(age)} old")
        data[label.replace(" ", "_")] = {"received_utc": snap.received_utc.isoformat(), "record_sha256": snap.record_sha256}
        if age > max_age:
            problems.append(f"the newest {label} read is {_age(age)} old")
    limit = f"(limit {_age(max_age)}; run python -m quant.execution.brokers.t212.pull)"
    detail = "; ".join(problems + parts) + f" {limit}" if problems else ", ".join(parts) + f" (limit {_age(max_age)})"
    return Check(name="snapshots", status="finding" if problems else "ok", detail=detail, data=data)


def check_rate_limit(rl: RateLimit | None, waits: Sequence[Wait], at: dt.datetime) -> Check:
    throttled = sum(1 for w in waits if w.status == 429)
    problems = [f"HTTP 429 met {throttled} time(s) before the read succeeded"] if throttled else []
    if rl is None:
        problems.append("the response carried no rate-limit headers: the client cannot pace by them")
        detail = ""
    else:
        to_reset = rl.seconds_to_reset(at)
        if to_reset > rl.period_s + RESET_SLACK_S:
            problems.append(f"the reset is {to_reset:.0f} s away, beyond the {rl.period_s} s period")
        if rl.remaining < rl.limit - 1:
            problems.append("other requests share the key's budget")
        detail = f"{rl.remaining} of {rl.limit} request(s) left in the {rl.period_s} s period after this check's one, reset in {to_reset:.0f} s"
    text = "; ".join([*problems, detail] if detail else problems) if problems else detail
    return Check(name="rate_limit", status="finding" if problems else "ok", detail=text,
                 data={"rate_limit": None if rl is None else rl.as_json(), "http_429": throttled})


def check_network(client: ReadClient) -> tuple[Check, Check]:
    """(reachability, rate_limit) from one journaled GET of the account summary."""
    try:
        pulled = client.account_summary()
    except ReadError as e:
        return (Check(name="reachability", status="finding", detail=f"{type(e).__name__}: {e}", data={"error": type(e).__name__}),
                Check(name="rate_limit", status="finding", detail="not measured: the read failed"))
    r = pulled.responses[-1]
    retries = sum(1 for w in client.waits if w.status is not None)
    reach = Check(name="reachability", status="ok", detail=f"GET {r.path}: HTTP {r.status} after {retries} retr{'y' if retries == 1 else 'ies'}, journal "
                                                           f"{(r.record_sha256 or 'none')[:12]}",
                  data={"path": r.path, "status": r.status, "retries": retries, "record_sha256": r.record_sha256, "received_utc": r.received_utc.isoformat()})
    return reach, check_rate_limit(client.rate_limits.get(ACCOUNT_SUMMARY.name), client.waits, r.received_utc)


def run(*, online: bool, journal_path: Path, now: dt.datetime, max_snapshot_age: dt.timedelta, client: Callable[[], ReadClient]) -> list[Check]:
    """Every check in order (module docstring). Snapshot freshness is measured before the network read; `client` is called only for that read."""
    keys = check_keys(online)
    checks = [keys, check_journal(journal_path), check_instrument_list(now, journal_path), check_snapshots(now, journal_path, max_snapshot_age)]
    if not online:
        return checks + [Check(name=n, status="info", detail="skipped: --offline") for n in ("reachability", "rate_limit")]
    if not keys.data["read_keys_ready"]:
        return checks + [Check(name="reachability", status="finding", detail="not attempted: the read keys are not available"),
                         Check(name="rate_limit", status="finding", detail="not measured: no read was made")]
    return checks + list(check_network(client()))


def main(argv: list[str] | None = None, now: dt.datetime | None = None, client: ReadClient | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quant.execution.brokers.t212.health", description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true", help="no network: keys are reported only; reachability and rate limit are skipped")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--journal", type=Path, default=journal.PATH)
    ap.add_argument("--max-snapshot-age-days", type=float, default=DEFAULT_MAX_SNAPSHOT_AGE_DAYS)
    a = ap.parse_args(argv)
    at = now or dt.datetime.now(dt.UTC)
    checks = run(online=not a.offline, journal_path=a.journal, now=at, max_snapshot_age=dt.timedelta(days=a.max_snapshot_age_days),
                 client=lambda: client or live_client(a.journal))
    findings = sum(c.status == "finding" for c in checks)
    if a.json:
        print(reports.dumps({"kind": "t212_health", "version": 1, "generated_utc": at.isoformat(), "mode": "offline" if a.offline else "online",
                             "findings": findings, "checks": [c.to_json() for c in checks]}), end="")
    else:
        for c in checks:
            print(f"{c.status.upper():8} {c.name:16} {c.detail}")
        print(f"health: {findings} finding(s) in {len(checks)} check(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
