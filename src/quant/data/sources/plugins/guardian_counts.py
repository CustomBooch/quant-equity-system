"""The Guardian's output, counted per month (`guardian_counts`): how many articles it published in all, in three sections and with four economic terms in them. The
Guardian Open Platform answers a count (`response.total`) without sending any article, so no text is stored or read.

One request per series per month: 8 series x about 142 months since 2015 is about 1,140 requests at one a second, three days of the developer key's quota (500 a day, 60 a minute; weekly rows
would take ten days); each answer is kept under data/raw/News/guardian, so a rerun after the quota stops a pull (HTTP 429) asks for nothing twice. A month's row is
only made once its last day has passed.

Needs a developer key in GUARDIAN_API_KEY (free, https://open-platform.theguardian.com/access/). Terms: the developer key is for non-commercial use (a paid key for
commercial use), with the Guardian credited in anything published: the licence row "Guardian Open Platform" is paper-trading and research only. `known` is the
last day of the month and the dataset declares a lag of one day.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

import pandas as pd

from quant.core.paths import REPO
from quant.data.sources.base import DataSource, Dataset, PullRequest
from quant.data.sources.registry import register

URL = "https://content.guardianapis.com/search"
CACHE = REPO / "data/raw/News/guardian"
# series name -> request filters: a section id, a search phrase, or neither (everything)
SERIES: dict[str, dict[str, str]] = {
    "n_all": {}, "n_business": {"section": "business"}, "n_world": {"section": "world"}, "n_politics": {"section": "politics"},
    "n_inflation": {"q": "inflation"}, "n_recession": {"q": "recession"}, "n_interest_rates": {"q": '"interest rates"'}, "n_tariffs": {"q": "tariffs"},
}


def today() -> dt.date:
    return dt.date.today()


def get(url: str) -> bytes:
    """The body of one GET request, then a pause of 1.1 s: the developer key allows 60 requests a minute and 500 a day. A timeout or an HTTP error (a 429 over
    the daily quota included) raises; nothing is retried."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "quant-research"}), timeout=60) as r:
        body: bytes = r.read()
    time.sleep(1.1)
    return body


def count(series: str, first: dt.date, last: dt.date) -> int:
    """Articles of `series` published from `first` to `last` inclusive: from the cache when asked before, else one request."""
    f = CACHE / f"{series}_{first:%Y%m%d}.json"
    if f.exists():
        return int(json.loads(f.read_text(encoding="utf-8")))
    key = os.environ.get("GUARDIAN_API_KEY")
    if not key:
        raise RuntimeError("guardian_counts: GUARDIAN_API_KEY is not set (a free developer key: https://open-platform.theguardian.com/access/)")
    query = urllib.parse.urlencode({"api-key": key, "from-date": f"{first:%Y-%m-%d}", "to-date": f"{last:%Y-%m-%d}", "page-size": 1, **SERIES[series]})
    body: dict[str, Any] = json.loads(get(f"{URL}?{query}"))["response"]
    if body.get("status") != "ok":
        raise RuntimeError(f"guardian_counts: the Guardian answered {body.get('status')}: {body.get('message')}")
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(body["total"]), encoding="utf-8")
    os.replace(tmp, f)                                       # whole or absent: a pull killed mid-write leaves no half-written file
    return int(body["total"])


@register
class GuardianCounts(DataSource):
    dataset = Dataset(name="guardian_counts", licence="Guardian Open Platform", entity="market", observed="month", known="published",
                      columns={s: "int64" for s in SERIES}, lag_days=1,
                      description="Guardian articles per calendar month: all, three sections and four economic terms")

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        first = pd.Timestamp(request.start)
        if first.day > 1:
            first = (first + pd.offsets.MonthBegin(1))        # a month is read whole, so the first is the first month that starts on or after the start
        rows = []
        while (last := first + pd.offsets.MonthEnd(0)).date() <= min(request.end, today() - dt.timedelta(days=1)):
            rows.append({"market": "guardian", "month": first, "published": last, **{s: count(s, first.date(), last.date()) for s in SERIES}})
            first = last + pd.Timedelta(days=1)
        return pd.DataFrame(rows, columns=["market", "month", "published", *SERIES])
