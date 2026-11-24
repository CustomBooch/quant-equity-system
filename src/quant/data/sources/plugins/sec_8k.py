"""SEC 8-K current reports (public filings; licence row "SEC EDGAR"): `sec_8k_events`, one row per company (CIK), report date and filing date, with the item numbers
filed (1.01, 2.02, 5.02 ...) and how many 8-Ks. Read from the local EDGAR submissions extract; no network, and no text: the item codes are the distilled part.

The company is the issuer's CIK, so no news-to-ticker tagging is involved. `known` is the EDGAR filing date, a real publication date. The data stops where the
local submissions extract stops (see the manifest's `known` range); refresh it with quant.data.sec.sec_pull before a pull that needs the newest filings.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data.sec.sec_pull import SUBS
from quant.data.sources.base import Dataset
from quant.data.sources.files import FileSource
from quant.data.sources.registry import register


def eight_ks(path: Path) -> pd.DataFrame:
    """The 8-K rows of the submissions extract: cik, accession, items, report_date and filing_date. A missing report date, or one after the filing date, becomes the
    filing date."""
    d = pd.read_parquet(path, columns=["cik", "accessionNumber", "filingDate", "reportDate", "form", "items"], filters=[("form", "==", "8-K")])
    d = d.rename(columns={"accessionNumber": "accession", "filingDate": "filing_date", "reportDate": "report_date"})
    d["report_date"] = d["report_date"].where(d["report_date"] <= d["filing_date"]).fillna(d["filing_date"])
    d["items"] = d["items"].fillna("")
    d["cik"] = d["cik"].astype(str)
    return d.drop(columns="form")


def by_company_and_day(d: pd.DataFrame) -> pd.DataFrame:
    """One row per (cik, report_date, filing_date): the items of all its 8-Ks (sorted, comma-separated) and their number."""
    return d.groupby(["cik", "report_date", "filing_date"], as_index=False).agg(
        items=("items", lambda s: ",".join(sorted({i for x in s for i in x.split(",") if i}))), n_filings=("accession", "size"))


@register
class Sec8kEvents(FileSource):
    dataset = Dataset(name="sec_8k_events", licence="SEC EDGAR", entity="cik", observed="report_date", known="filing_date",
                      columns={"items": "string", "n_filings": "int64"},
                      description="SEC 8-K filings per company and day: the item numbers filed (to the end of the local submissions extract)")
    path = SUBS

    def read(self) -> pd.DataFrame:
        if not self.path.is_file():
            raise FileNotFoundError(f"{self.dataset.name}: EDGAR submissions extract not found: {self.path}")
        return by_company_and_day(eight_ks(self.path))
