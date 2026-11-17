"""The broker of the PAPER path (lab/paper/SPEC.md amendment A10, ratified 2026-11-12): the Alpaca paper account in USD, the only one since amendment A14.
Targets, preflight and the engine read this one setting; the engine refuses a plan made for any other broker."""
from __future__ import annotations

import re

from quant.portfolio.model import MODEL

BROKER = MODEL.execution.broker
SHORTS = MODEL.execution.shorts                             # amendment A11: the Alpaca book carries the short leg
MAX_SHORT_FRAC = 0.30                                       # a control (engine and rust_core), not a model parameter: the model's high short weight must stay within it
MAX_GROSS = 2.0                                             # A12 control: leverage k x (1 + 2 s) at most this (Reg-T overnight limit on the margin account)
MAX_SHORT_LEG = 0.40                                        # A12 control: short sales opened per tranche at most this x account / K x 1.05 (k s <= 0.375)
_ALPACA = re.compile(r"[A-Z]{1,5}(\.[A-Z]{1,2})?")


def line_column(broker: str = BROKER) -> str:
    """The target-book column that holds the broker's instrument line (`line`: the Alpaca symbol)."""
    if broker != "alpaca":
        raise ValueError(f"unknown broker {broker!r}: only alpaca exists (amendment A14)")
    return "line"


def alpaca_symbol(s: str) -> bool:
    """1-5 capital letters with an optional 1-2 letter share class after a dot: the same rule as rust_core's `execution::alpaca_symbol`."""
    return bool(_ALPACA.fullmatch(s))


def usd_line(ticker: str) -> bool:
    """Whether `ticker` is an Alpaca US equity symbol."""
    return alpaca_symbol(ticker)
