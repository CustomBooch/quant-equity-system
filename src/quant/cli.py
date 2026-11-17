"""The command line of the package: one table naming the commands of the monthly cycle (the wiki page Home).

    tools/py -m quant                      # the list below
    tools/py -m quant score --month 2026-11-01
    tools/py -m quant engine plan TARGET.csv --account-usd 100000 --slot 2026-11-17
    tools/py -m quant engine reconcile lab/paper/plans/<hash>.json     # a recorded name: found under record/ from any folder

Each command is a module with its own options (`tools/py -m quant <command> --help`); this file only names them in the order they are run. A command
that reads an account needs the keys: start it with `dotenvx run -- tools/py -m quant <command>`. Nothing here sends an order: only `engine send` can,
and only with the owner's token for that plan (the wiki page Home). Installed (`pip install -e .`), the same table is the `quant` command.
"""
from __future__ import annotations

import importlib.util
import runpy
import sys
from typing import NamedTuple

from quant.core.paths import argument


class Command(NamedTuple):
    module: str
    does: str


COMMANDS = {
    # once a month: data, release, score, book
    "bars": Command("quant.data.yahoo.yahoo_bars", "refresh the Yahoo daily bars"),
    "assemble": Command("quant.features.ciqfill.assemble", "build a master release from the engine build and the bridge-month sources"),
    "release": Command("quant.core.release.pointer", "show, promote or roll back the current master release"),
    "score": Command("quant.models.scores", "score a decision month with the model on the current master"),
    "targets": Command("quant.portfolio.targets", "build the target book from the newest scores"),
    "model": Command("quant.portfolio.model", "print the traded model's parameters"),
    # every Tuesday slot: check, plan, send, reconcile
    "alpaca-assets": Command("quant.execution.brokers.alpaca.pull_assets", "refresh the Alpaca asset list (read only)"),
    "preflight": Command("quant.execution.preflight", "go / no-go checks before a batch"),
    "engine": Command("quant.execution.engine", "the order path: plan, send, reconcile, verify-audit"),
    "borrow-watch": Command("quant.ops.borrow_watch", "alert on held shorts that are no longer easy to borrow (local files only)"),
    # infrastructure
    "data": Command("quant.data.sources.cli", "the registered data sources: list, show, pull, check (the wiki page Adding-a-dataset)"),
    "licences": Command("quant.core.licences", "what each data source may be used for, and what real-money use would first need"),
    "kernels": Command("quant.core.kernels", "build or report the Rust kernels (build, status)"),
    "registry": Command("quant.core.registry", "the experiment registry: verify, query, import"),
}


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    rows = [f"  {name:<{width}}  {c.does}" for name, c in COMMANDS.items()]
    return "\n".join(["usage: tools/py -m quant <command> [options]", "", *rows, "", "Options of a command: tools/py -m quant <command> --help"])


def main(argv: list[str] | None = None) -> int:
    """Run the named command as if its module had been started with -m; 0 after the list, 2 for an unknown command."""
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    name = args[0]
    if name not in COMMANDS:
        print(f"unknown command {name!r}\n\n{usage()}", file=sys.stderr)
        return 2
    sys.argv = [f"quant {name}", *(argument(a) for a in args[1:])]      # a recorded name (lab/..., data/...) typed at the checkout is found under record/
    runpy.run_module(COMMANDS[name].module, run_name="__main__", alter_sys=True)
    return 0


def missing() -> list[str]:
    """Commands whose module cannot be found (tested: the list never names a module that moved)."""
    return [name for name, c in COMMANDS.items() if importlib.util.find_spec(c.module) is None]
