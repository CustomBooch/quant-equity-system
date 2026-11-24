"""The San Francisco Fed Daily News Sentiment Index (`sf_fed_news_sentiment`): one figure a day, 1980 on, the day fixed effect of a sentiment score taken over the
economics-related articles of 24 US newspapers (Buckman, Shapiro, Sudhof and Wilson, "News Sentiment in the Time of COVID-19", FRBSF Economic Letter 2020). It is
distilled by its authors; no text is read here. Read from the Fed's spreadsheet (the Data sheet: an Excel date and the figure), with the standard library only.

`known` is the day itself and the dataset declares a lag of 3 days: the spreadsheet runs to within a few days of its last update. The authors re-estimate the whole
series with each update, so past days are revised: use it as a state variable, not as a precise historical signal. Terms: a Federal Reserve Bank publication, free to
use with the source credited; the licence row "SF Fed news sentiment" does not confirm real-money use.
"""
from __future__ import annotations

import datetime as dt
import io
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

from quant.data.sources.base import DataSource, Dataset, PullRequest
from quant.data.sources.base import http_get as get
from quant.data.sources.registry import register

URL = "https://www.frbsf.org/wp-content/uploads/news_sentiment_data.xlsx"
SHEET = "xl/worksheets/sheet2.xml"                           # the Data sheet (the first sheet is the methodology note)
MAX_BYTES = 50 << 20                                          # a sheet that unpacks to more than this is refused
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
EXCEL_DAY0 = dt.date(1899, 12, 30)                            # the day an Excel serial date counts from


def read_sheet(xlsx: bytes) -> pd.DataFrame:
    """The Data sheet as date and sentiment: each row of two numbers is a day (an Excel serial date) and its figure; the header and any other row are skipped."""
    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        info = z.getinfo(SHEET)
        if info.file_size > MAX_BYTES:
            raise ValueError(f"{SHEET} unpacks to {info.file_size} bytes, above the {MAX_BYTES} limit")
        root = ET.fromstring(z.read(info))
    rows = []
    for row in root.iter(f"{NS}row"):
        cells = [c for c in row.findall(f"{NS}c") if c.get("t") in (None, "n") and c.find(f"{NS}v") is not None]
        if len(cells) == 2:
            serial, value = (float(c.findtext(f"{NS}v") or "nan") for c in cells)
            rows.append((pd.Timestamp(EXCEL_DAY0) + pd.Timedelta(days=int(serial)), value))
    return pd.DataFrame(rows, columns=["date", "sentiment"])


@register
class SfFedNewsSentiment(DataSource):
    dataset = Dataset(name="sf_fed_news_sentiment", licence="SF Fed news sentiment", entity="market", observed="date", known="published",
                      columns={"sentiment": "float64"}, lag_days=3,
                      description="San Francisco Fed daily news sentiment index (24 US newspapers, economics articles), 1980 on")

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        d = read_sheet(get(URL))
        if d.empty:
            raise ValueError("sf_fed_news_sentiment: the spreadsheet has no data rows (its layout may have changed)")
        d["published"] = d["date"]
        d["market"] = "sffed"
        d = d[(d["date"] >= pd.Timestamp(request.start)) & (d["date"] <= pd.Timestamp(request.end))]
        return d[["market", "date", "published", "sentiment"]]
