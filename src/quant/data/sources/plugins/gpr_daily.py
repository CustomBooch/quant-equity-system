"""The Caldara-Iacoviello Geopolitical Risk index, daily (`gpr_daily`): the share of articles in 10 large newspapers that discuss adverse geopolitical events (Caldara
and Iacoviello, "Measuring Geopolitical Risk", American Economic Review 2022), 1985 on: the overall index and its two parts, geopolitical acts (war, terror) and
threats. Distilled by its authors; no text is read here. Read from the author's spreadsheet (an old-format .xls, so this needs xlrd, pinned in the core lock).

`known` is the day itself and the dataset declares a lag of 2 days: the file is updated a day behind. The authors revise the series when they update it, so use it
as a state variable, not as a precise historical signal. Terms: free for academic and other use citing the authors (https://www.matteoiacoviello.com/gpr.htm);
the licence row "Geopolitical Risk index" does not confirm real-money use.
"""
from __future__ import annotations

import io
import urllib.request

import pandas as pd

from quant.data.sources.base import DataSource, Dataset, PullRequest
from quant.data.sources.registry import register

URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls"
MAX_BYTES = 50 << 20                                          # a download larger than this is refused
COLUMNS = {"GPRD": "gpr", "GPRD_ACT": "gpr_acts", "GPRD_THREAT": "gpr_threats"}


def get(url: str) -> bytes:
    """The body of one GET request (a timeout or an HTTP error raises; nothing is retried)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "quant-research"}), timeout=120) as r:
        body: bytes = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"{url} is larger than the {MAX_BYTES} byte limit")
    return body


@register
class GprDaily(DataSource):
    dataset = Dataset(name="gpr_daily", licence="Geopolitical Risk index", entity="market", observed="date", known="published",
                      columns={"gpr": "float64", "gpr_acts": "float64", "gpr_threats": "float64"}, lag_days=2,
                      description="Caldara-Iacoviello daily geopolitical risk index (10 newspapers), overall, acts and threats, 1985 on")

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        d = pd.read_excel(io.BytesIO(get(URL)), engine="xlrd")
        missing = {"DAY", *COLUMNS} - set(d.columns)
        if missing:
            raise ValueError(f"gpr_daily: the spreadsheet has no column {sorted(missing)} (its layout may have changed)")
        d = d[["DAY", *COLUMNS]].dropna(subset=["DAY"]).rename(columns=COLUMNS)
        d["date"] = pd.to_datetime(d["DAY"].astype("int64").astype(str), format="%Y%m%d")
        d["published"] = d["date"]
        d["market"] = "gpr"
        d = d[(d["date"] >= pd.Timestamp(request.start)) & (d["date"] <= pd.Timestamp(request.end))]
        return d[["market", "date", "published", *COLUMNS.values()]]
