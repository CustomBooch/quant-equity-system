"""The book's parameters in one place. Every other part of the order path reads them from here: quant.portfolio.targets (long book),
quant.portfolio.broker (broker, short leg on or off).

    python -m quant.portfolio.model        # prints the model card

The values below are an EXAMPLE (a long-only, unlevered top-decile book) chosen to exercise the code, not a recommendation. The safety limits (order cap,
short cap, buy limit, whole-share shorts) live in quant.execution.engine and rust_core: they are controls, not model.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class LongBook:
    """Buy the top decile of the score, hold until it falls below the 70th percentile; value weights, 5% cap; 5 bp a side."""
    score: str = "EXAMPLE"
    entry: float = 0.90
    exit: float = 0.70
    sizing: str = "vw"
    cap: float = 0.05
    cost_bp: float = 5.0


@dataclass(frozen=True)
class ShortBasket:
    """Not used by the example (Execution.shorts is False); kept so the parameters have one shape."""
    pool: str = "none"
    rank: str = "-score"
    exclude_reported_months_ago: int = 0
    entry: float = 0.90
    exit: float = 0.70
    sizing: str = "vw"
    cap: float = 0.05
    tradable_requires: tuple[str, ...] = ("active", "tradable", "shortable", "easy_to_borrow")


@dataclass(frozen=True)
class ShortSize:
    """Short weight s; zero in the example."""
    high: float = 0.0
    low: float = 0.0
    full_step: float = 0.10
    lookback_months: int = 6
    fallback: float = 0.0


@dataclass(frozen=True)
class Leverage:
    """Both legs are scaled by k = gross_cap / (1 + 2 s): longs k (1 + s) plus shorts k s equal gross_cap before the overlay. 1.0: unlevered."""
    gross_cap: float = 1.0


@dataclass(frozen=True)
class Overlay:
    """Binary trend overlay on a total-market series; mirrored from quant.portfolio.schedule."""
    signal: str = "VTI month-end close below its 10-month mean"
    defensive_exposure: float = 0.5


@dataclass(frozen=True)
class Execution:
    broker: str = "alpaca"
    shorts: bool = False
    tranches: int = 4
    weekday: str = "Tuesday"


@dataclass(frozen=True)
class Model:
    name: str = "EXAMPLE: long-only top decile of the example score, value weights (5% cap), unlevered, trend overlay"
    long: LongBook = field(default_factory=LongBook)
    short: ShortBasket = field(default_factory=ShortBasket)
    size: ShortSize = field(default_factory=ShortSize)
    leverage: Leverage = field(default_factory=Leverage)
    overlay: Overlay = field(default_factory=Overlay)
    execution: Execution = field(default_factory=Execution)


MODEL = Model()


def short_fraction(lead: float | None, size: ShortSize = MODEL.size) -> tuple[float, str]:
    """(s, rule): `high` at a lead <= 0, linearly to `low` at a lead >= `full_step`; `fallback` when the lead is unknown."""
    if lead is None or not math.isfinite(lead):
        return size.fallback, f"{size.lookback_months}-month lead unavailable: s = {size.fallback:.2f}"
    x = min(max(lead / size.full_step, 0.0), 1.0)
    return size.high - (size.high - size.low) * x, f"gradual switch on a {size.lookback_months}-month lead of {lead:+.4f}"


def leverage(s: float, lev: Leverage = MODEL.leverage) -> float:
    """k for short weight s: longs k (1 + s) and shorts k s add up to the gross cap."""
    return lev.gross_cap / (1 + 2 * s)


def long_policy(book: LongBook = MODEL.long) -> dict[str, Any]:
    """The long book in quant.portfolio.targets' policy format."""
    return {"entry": book.entry, "exit": book.exit, "cap": book.cap, "sizing": book.sizing}


def card(m: Model = MODEL) -> str:
    return json.dumps(asdict(m), indent=1)


if __name__ == "__main__":
    print(card())
