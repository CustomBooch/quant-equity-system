"""A source read from a file someone exports by hand (a vendor download, a library workbook): CSV, Parquet or Excel.

Subclass FileSource, set `dataset`, `path` and, if the file's headers differ from the declared names, `rename` ({file header: declared name}).
The file is read on every pull, filtered to the request's dates and entities, and stored like any other pull.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pandas as pd

from quant.data.sources.base import DataSource, PullRequest


class FileSource(DataSource):
    path: ClassVar[Path]
    rename: ClassVar[dict[str, str]] = {}

    def read(self) -> pd.DataFrame:
        p = self.path
        if not p.is_file():
            raise FileNotFoundError(f"{self.dataset.name}: export file not found: {p}")
        if p.suffix.lower() == ".parquet":
            return pd.read_parquet(p)
        if p.suffix.lower() in (".xlsx", ".xls"):
            return pd.read_excel(p)
        return pd.read_csv(p)

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        ds = self.dataset
        frame = self.read().rename(columns=self.rename)
        obs = pd.to_datetime(frame[ds.observed])
        keep = (obs >= pd.Timestamp(request.start)) & (obs <= pd.Timestamp(request.end))
        if request.entities is not None:
            keep &= frame[ds.entity].astype(str).isin(request.entities)
        return frame[keep]
