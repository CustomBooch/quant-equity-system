"""What a data source declares, and the checks every pull passes before anything is stored.

A source is one class: a `Dataset` (what the data is) and a `fetch` method (how to get it). Everything else, the checks, the store, the manifest, the
point-in-time load and the monthly join, is shared (`quant.data.sources.store`, `quant.features.sources`). the wiki page Adding-a-dataset walks through one.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import ClassVar

import pandas as pd

ENTITIES = ("permno", "ticker", "cik", "cusip", "market")       # the key a row is about; "market" for one series per date (an index, a macro figure)
DTYPES = ("float64", "int64", "string", "bool", "date")


@dataclass(frozen=True)
class Dataset:
    """The declaration of one dataset.

    name         lower_snake_case; the store folder (record/data/sources/<name>) and the command-line name
    licence      the name of its row in quant.core.licences.SOURCES (the terms it may be used under); a contract test checks the row exists
    entity       the column that names what a row is about, one of ENTITIES (its column holds that identifier)
    observed     the column with the date the value describes (a fiscal period end, a trade date)
    known        the column with the date the value became public (a filing or publish date). The as-of join uses only rows known before the
                 decision date. If the source gives no such date, set `known` to `observed` and `lag_days` to a delay you can defend.
    columns      the value columns and their types (each one of DTYPES), besides entity, observed and known
    lag_days     days added to `known` before a row may be used (0 when `known` is a real publication date)
    description  one sentence: what it is and where it comes from
    """
    name: str
    licence: str
    entity: str
    observed: str
    known: str
    columns: dict[str, str] = field(default_factory=dict)
    lag_days: int = 0
    description: str = ""

    def problems(self) -> list[str]:
        """What is wrong with the declaration itself; [] when it is sound."""
        out = []
        if not self.name.replace("_", "").isalnum() or self.name != self.name.lower() or not self.name[:1].isalpha():
            out.append(f"name {self.name!r} is not lower_snake_case")
        if self.entity not in ENTITIES:
            out.append(f"entity {self.entity!r} is not one of {ENTITIES}")
        reserved = {self.entity, self.observed, self.known}
        bad = {c: t for c, t in self.columns.items() if t not in DTYPES}
        if bad:
            out.append(f"column types not in {DTYPES}: {bad}")
        clash = sorted(reserved & set(self.columns))
        if clash:
            out.append(f"value columns repeat a key column: {clash}")
        if not self.columns:
            out.append("no value columns")
        if self.lag_days < 0:
            out.append("lag_days is negative")
        if self.known == self.observed and self.lag_days == 0:
            out.append("`known` is the observation date with no lag: the value would be used on the day it describes (state a lag_days)")
        return out

    @property
    def key(self) -> list[str]:
        return [self.entity, self.observed, self.known]


@dataclass(frozen=True)
class PullRequest:
    """What one pull asks for: observations from `start` to `end`, optionally only these entities (None: all the source offers)."""
    start: dt.date
    end: dt.date
    entities: tuple[str, ...] | None = None


class DataSource:
    """Base class of a source. A subclass sets `dataset` and implements `fetch`; `register` (quant.data.sources.registry) makes it known.

    `fetch` returns a frame with the entity, observed and known columns and every declared value column. It may call the network; nothing else here does.
    """
    dataset: ClassVar[Dataset]

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        raise NotImplementedError


def conform(frame: pd.DataFrame, ds: Dataset) -> pd.DataFrame:
    """The frame with exactly the declared columns in declared order and types (dates as datetime64 at midnight); a missing column raises KeyError."""
    missing = [c for c in [*ds.key, *ds.columns] if c not in frame.columns]
    if missing:
        raise KeyError(f"{ds.name}: missing columns {missing}")
    out = pd.DataFrame(index=frame.index)
    out[ds.entity] = frame[ds.entity].astype("string")
    for c in (ds.observed, ds.known):
        out[c] = pd.to_datetime(frame[c]).dt.normalize()
    for c, t in ds.columns.items():
        out[c] = pd.to_datetime(frame[c]).dt.normalize() if t == "date" else frame[c].astype(t)  # type: ignore[call-overload]  # t is one of DTYPES
    return out.reset_index(drop=True)


def validate(frame: pd.DataFrame, ds: Dataset, today: dt.date | None = None) -> list[str]:
    """Every reason `frame` (already conformed) may not be stored; [] when it passes.

    The point-in-time rules: a value cannot be known before the date it describes, nor after today; keys are unique; no entity or date is missing.
    """
    out = []
    today_ts = pd.Timestamp(today or dt.date.today())
    for c in ds.key:
        n = int(frame[c].isna().sum())
        if n:
            out.append(f"{n} rows without {c}")
    early = frame[frame[ds.known] < frame[ds.observed]]
    if len(early):
        out.append(f"{len(early)} rows known before the date they describe (first: {early.iloc[0][ds.entity]} {early.iloc[0][ds.observed].date()})")
    future = frame[frame[ds.known] > today_ts]
    if len(future):
        out.append(f"{len(future)} rows known after today ({today_ts.date()})")
    dup = int(frame.duplicated(ds.key).sum())
    if dup:
        out.append(f"{dup} duplicate rows on {ds.key}")
    return out
