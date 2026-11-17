"""Read the Trading 212 practice account and journal every response (quant.execution.brokers.t212.client, quant.execution.brokers.t212.journal). Read-only: GET to the practice host with the read keys.

    dotenvx run -- tools/py -m quant.execution.brokers.t212.pull                          # account summary and positions: what reconciliation and fill analysis read
    dotenvx run -- tools/py -m quant.execution.brokers.t212.pull positions dividends      # named reads; `all` for every one
    reads: account_summary positions dividends transactions exports exchanges instruments

One line per read: records read, responses, and the journal records that hold them. The read keys are checked first: if they are missing or still encrypted
nothing is requested. A read that fails is printed with its error and the others still run. Exit code 0 if every read completed, 1 otherwise.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from quant.execution.brokers.t212.api import auth
from quant.execution.brokers.t212.client import Pulled, ReadClient, ReadError, live_client

READS: dict[str, Callable[[ReadClient], Pulled[Any]]] = {
    "account_summary": lambda c: c.account_summary(),
    "positions": lambda c: c.positions(),
    "dividends": lambda c: c.dividends(),
    "transactions": lambda c: c.transactions(),
    "exports": lambda c: c.exports(),
    "exchanges": lambda c: c.exchanges(),
    "instruments": lambda c: c.instruments(),
}
DEFAULT = ("account_summary", "positions")


def describe(name: str, pulled: Pulled[Any]) -> str:
    """One line about a completed read."""
    n = len(pulled.data) if isinstance(pulled.data, tuple) else 1
    return f"{name}: {n} record(s) from {len(pulled.responses)} response(s); journal {', '.join(s[:12] for s in pulled.sources) or 'not written'}"


def run(names: Sequence[str], client: ReadClient) -> tuple[int, list[str]]:
    """Run the named reads in order; (exit code, one line per read)."""
    lines, failed = [], False
    for name in names:
        try:
            lines.append(describe(name, READS[name](client)))
        except ReadError as e:
            failed = True
            lines.append(f"FAILED {name}: {type(e).__name__}: {e}")
    return (1 if failed else 0), lines


def main(argv: list[str] | None = None, client: ReadClient | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quant.execution.brokers.t212.pull", description=__doc__.split("\n")[0])
    ap.add_argument("reads", nargs="*", metavar="read", help=f"one of {', '.join(READS)}, or all; default: {' '.join(DEFAULT)}")
    a = ap.parse_args(argv)
    if unknown := [r for r in a.reads if r not in READS and r != "all"]:      # checked here: argparse's `choices` refuses an empty list in Python 3.11
        ap.error(f"unknown read(s) {unknown}; choose from {', '.join(READS)}, all")
    names = list(READS) if "all" in a.reads else (a.reads or list(DEFAULT))
    try:
        auth("READ")                                        # refuse before any request when the read keys are not in the environment
    except RuntimeError as e:
        print(f"REFUSED: {e}")
        return 1
    code, lines = run(names, client or live_client())
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
