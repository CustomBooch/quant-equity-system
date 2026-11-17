"""READY pre-flight: a read-only go / no-go check to run before any trading step. python -m quant.execution.preflight [--no-account]
Checks (nothing is written except results/preflight.json; no order endpoint exists in this module):
  1. data freshness: latest scored month of the traded score against today; the READY rule is that a score may be at most one month old (stale-input backtest, rails.py). Since PAPER amendment A4
     the traded score is EXAMPLE from quant.models.scores (the frozen C3 ledger month is still reported, for information only)
  2. frozen artefacts intact
  3. instrument list age and mapping coverage of the latest scored top decile (rails: refuse if more than 5% of target weight is unmappable)
  4. Alpaca paper account state through GET (status, shorting, margin, equity, positions), if keys are present
  5. Yahoo analyst archive age
  6. the PAPER audit log's hash chain (quant.execution.audit); the newest hash is reported, so a cut at the end of the log shows against the last report
Exit code 0 on GO, 1 on NO-GO; quant.execution.engine trusts only a run that exited 0.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

import pandas as pd

from quant.core.frozen import check_frozen
from quant.execution import audit
from quant.core.paths import REPO
from quant.portfolio.broker import BROKER, SHORTS, line_column
from quant.portfolio.model import MODEL

MAX_SCORE_AGE_MONTHS = 1
MAX_UNMAPPED_WEIGHT = 0.05
RES = REPO / "lab/ready/results"

Check = Callable[[dt.date], tuple[bool, str]]        # one check: given the run date, (passed, detail); every check takes the date, used or not
CheckResult = TypedDict("CheckResult", {"pass": bool, "detail": str})


def latest_paper_scores() -> str | None:
    """Newest decision month with a EXAMPLE score file (PAPER amendment A4: the score that is traded)."""
    from quant.models import scores as SC

    r = SC.latest()
    return r[0] if r else None


def paper_mapping() -> dict[str, Any]:
    """The primary book's entry names on the newest score file (quant.portfolio.targets rules) and the share of their target weight with no line at the broker in use."""
    from quant.models import scores as SC
    from quant.portfolio import targets as TG

    latest = SC.latest()
    if latest is None:
        raise FileNotFoundError(f"no EXAMPLE score file in {SC.OUT}")
    _, f = latest
    pol = TG.POLICIES[TG.PRIMARY]
    col = line_column(BROKER)
    sel = TG.map_lines(TG.select(pd.read_parquet(f).set_index("permno").rename(columns={"me_lag": "mcap"}), pol), BROKER)
    return {"selected": int(len(sel)), "mapped": int(sel[col].notna().sum()), "weight_dropped_before_renormalisation": TG.unmapped_share(sel, pol, col)}


def latest_scored_month() -> str | None:
    """Newest decision month in the frozen C3 V0 ledgers (information only since amendment A4)."""
    best = None
    for root in ("ledger_R", "ledger_R_supp"):
        for f in glob.glob(str(REPO / "lab/prospective" / root / "preds/*__V0.parquet")):
            m = f.replace("\\", "/").split("/")[-1].split("__")[0]
            best = max(best or m, m)
    return best


def months_between(a: str, b: dt.date) -> float:
    """Months from date `a` (YYYY-MM-DD) to `b`, with a 30-day month for the days."""
    start = pd.Timestamp(a)
    return (b.year - start.year) * 12 + (b.month - start.month) + (b.day - start.day) / 30.0


def instrument_list_date() -> str | None:
    """Pull date of the newest Alpaca asset list (amendment A10), or None if there is none."""
    from quant.data.instruments import alpaca as AM

    return AM.list_date()


def score_freshness(today: dt.date) -> tuple[bool, str]:
    """Check 1: the newest EXAMPLE decision month is the current month or the previous one."""
    sm = latest_paper_scores()
    if not sm:
        return False, "no scored month found"
    age = months_between(sm, today)              # a score for decision month M uses inputs through the end of M-1, so its age is measured from the start of M
    if age < 0:
        return False, f"latest EXAMPLE decision month {sm} is after today ({today}): a score cannot be dated in the future"
    return age <= MAX_SCORE_AGE_MONTHS + 1.0, f"latest EXAMPLE decision month {sm}; age {age:.1f} months from the start of that month (rule: decision month must be the current month or the previous one)"


def frozen_artefacts(today: dt.date) -> tuple[bool, str]:
    """Check 2: every hashed entry of the freeze manifest is intact."""
    fr = check_frozen()
    return fr["intact"], f"{fr['entries_checked']} hashed entries intact" if fr["intact"] else f"changed: {fr['mismatches']}; missing: {fr['missing']}"


def instrument_list_age(today: dt.date) -> tuple[bool, str]:
    """Check 3a: an Alpaca asset list at most 31 days old exists."""
    d = instrument_list_date()
    pull = "dotenvx run -- python -m quant.execution.brokers.alpaca.pull_assets"
    return (False, f"no instrument list; run {pull}") if d is None else ((today - dt.date.fromisoformat(d)).days <= 31, f"{BROKER} instrument list dated {d}")


def mapping_coverage(today: dt.date) -> tuple[bool, str]:
    """Check 3b: at most MAX_UNMAPPED_WEIGHT of the primary book's target weight has no Alpaca symbol (not measured counts as failed)."""
    if instrument_list_date() is None or latest_paper_scores() is None:
        return False, "not measured: no instrument list or no score file"
    info = paper_mapping()
    share = info["weight_dropped_before_renormalisation"]
    return share <= MAX_UNMAPPED_WEIGHT, f"{info['mapped']} of {info['selected']} names mapped; {share:.2%} of target weight unmappable (limit {MAX_UNMAPPED_WEIGHT:.0%})"


def account_reachable(today: dt.date) -> tuple[bool, str]:
    """Check 4: the Alpaca paper account answers its GETs (amendment A10): status ACTIVE, not blocked, in USD."""
    from quant.execution.brokers.alpaca import api as ALP

    acct = ALP.account()
    problems = ALP.account_problems(acct)
    if SHORTS and acct.get("shorting_enabled") is not True:
        problems.append("shorting_enabled is not true (amendment A11 needs short selling)")
    if MODEL.leverage.gross_cap > 1 and float(acct.get("multiplier") or 1) < 2:
        problems.append(f"margin multiplier {acct.get('multiplier')!r} below 2 (amendment A12 holds gross up to {MODEL.leverage.gross_cap})")
    if problems:
        return False, f"Alpaca paper account not tradable: {'; '.join(problems)}"
    return True, f"Alpaca paper account read by GET only; status ACTIVE{', shorting enabled' if SHORTS else ''}, equity {ALP.equity_usd(acct):,.2f} USD; {len(ALP.positions())} open position(s)"


def analyst_archive_age(today: dt.date) -> tuple[bool, str]:
    """Check 5: a Yahoo analyst snapshot at most 35 days old exists."""
    files = sorted(glob.glob(str(REPO / "data/raw_ext/yahoo_analyst_archive/snapshot_*.parquet")))
    d = Path(files[-1]).name.removeprefix("snapshot_")[:10] if files else None
    return (False, "no archive") if d is None else ((today - dt.date.fromisoformat(d)).days <= 35, f"latest Yahoo analyst snapshot {d}")


def audit_log_chain(today: dt.date) -> tuple[bool, str]:
    """Check 6: the PAPER audit log's hash chain is intact; the newest hash is reported."""
    ch = audit.verify()
    if ch["anchor_problem"]:
        return False, ch["anchor_problem"]
    return not ch["broken"], f"broken at line(s) {ch['broken']}" if ch["broken"] else f"{ch['records']} records ({ch['unchained']} before the chain began); newest hash {ch['head']}"


CHECKS: tuple[Check, ...] = (score_freshness, frozen_artefacts, instrument_list_age, mapping_coverage, account_reachable, analyst_archive_age, audit_log_chain)


def run_checks(today: dt.date, account: bool = True) -> dict[str, CheckResult]:
    """Every check in CHECKS (the account check only if `account`), by name: {"pass", "detail"}. A check that raises fails with its error as the detail, so one broken
    input is reported as a failed check instead of stopping the preflight before it writes its result."""
    out: dict[str, CheckResult] = {}
    for check in CHECKS:
        if check is account_reachable and not account:
            continue
        try:
            passed, detail = check(today)
        except Exception as e:                                                      # noqa: BLE001 - a check that cannot run has not passed
            passed, detail = False, repr(e)[:200]
        out[check.__name__] = {"pass": bool(passed), "detail": detail}
    return out


def main(argv: list[str] | None = None, *, today: dt.date | None = None) -> int:
    """Run the checks for `today` (default: the wall-clock date, which quant.execution.engine requires of a run it trusts), write results/preflight.json and
    print the verdict; 0 on GO, 1 on NO-GO."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-account", action="store_true")
    a = ap.parse_args(argv)
    today = dt.date.today() if today is None else today
    checks = run_checks(today, account=not a.no_account)
    ok = all(v["pass"] for v in checks.values())
    out = {"run_date": str(today), "go": ok, "checks": checks, "info": {"c3_v0_ledger_latest_month": latest_scored_month()},
           "note": "read-only; 'go' means every check passed, not that trading is advisable"}
    (RES / "preflight.json").write_text(json.dumps(out, indent=1) + "\n")
    for k, v in checks.items():
        print(("PASS " if v["pass"] else "FAIL ") + k + ": " + v["detail"])
    print("GO" if ok else "NO-GO")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
