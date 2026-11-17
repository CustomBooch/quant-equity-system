"""Monthly features from any registered data source, point in time.

    monthly_asof(rows, dataset, months, link=None, max_age_days=None, prefix=None) -> one row per (permno, month)

For decision month M the data runs to the last day of M - 1 (as everywhere in the panel): each entity's newest row usable by then (its `known` date plus
the dataset's `lag_days`) gives the values for M. A row older than `max_age_days` at that date is dropped, so a firm that stopped reporting does not carry
an old value forever. Datasets keyed by ticker, CIK or CUSIP need `link`: rows (id, permno, start, end) saying which permno an identifier named and when.
"""
from __future__ import annotations

import pandas as pd

from quant.data.sources.base import Dataset
from quant.data.sources.store import usable_from


def to_permno(rows: pd.DataFrame, ds: Dataset, link: pd.DataFrame | None) -> pd.DataFrame:
    """The rows keyed by permno: as they are for a permno dataset; through `link` (id, permno, start, end) at each row's usable date otherwise."""
    if ds.entity == "permno":
        return rows.assign(permno=rows["permno"].astype("int64"))
    if link is None:
        raise ValueError(f"{ds.name} is keyed by {ds.entity}: pass `link` (id, permno, start, end) to map it to permnos")
    j = rows.merge(link.rename(columns={"id": ds.entity}), on=ds.entity, how="inner")
    when = j["_usable"]
    j = j[(when >= pd.to_datetime(j["start"])) & (when <= pd.to_datetime(j["end"]).fillna(pd.Timestamp.max))]
    return j.drop(columns=["start", "end"]).assign(permno=lambda d: d["permno"].astype("int64"))


def monthly_asof(rows: pd.DataFrame, ds: Dataset, months: list[pd.Timestamp] | pd.DatetimeIndex, link: pd.DataFrame | None = None,
                 max_age_days: int | None = None, prefix: str | None = None, columns: list[str] | None = None) -> pd.DataFrame:
    """(permno, month, <prefix>_<column>...) for each decision month: the newest value usable by the end of the month before."""
    if ds.entity == "market":
        raise ValueError(f"{ds.name} is a market series: join it on the date, not per stock")
    cols = columns or list(ds.columns)
    pre = (prefix if prefix is not None else ds.name) + "_"
    r = rows.assign(_usable=usable_from(rows, ds.known, ds.lag_days))
    r = to_permno(r, ds, link).sort_values("_usable")
    out = []
    for m in sorted(pd.to_datetime(pd.Index(months)).unique()):
        cut = m - pd.Timedelta(days=1)                     # the data cut-off of decision month m: the last day of m - 1
        live = r[r["_usable"] <= cut]
        if max_age_days is not None:
            live = live[live["_usable"] > cut - pd.Timedelta(days=max_age_days)]
        last = live.drop_duplicates("permno", keep="last")
        out.append(last[["permno", *cols]].assign(month=m))
    if not out:
        return pd.DataFrame(columns=["permno", "month", *(pre + c for c in cols)])
    res = pd.concat(out, ignore_index=True).rename(columns={c: pre + c for c in cols})
    return res[["permno", "month", *(pre + c for c in cols)]].sort_values(["month", "permno"]).reset_index(drop=True)
