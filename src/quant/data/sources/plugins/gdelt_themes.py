"""GDELT global news, distilled to one row per day (`gdelt_themes`): how many articles, their average tone, and for 12 economic themes how many articles carry the
theme and their average tone. GDELT scores every article itself (theme tags and a tone figure), so no text is read here.

One 15-minute GKG 2.1 file a day is read (the first of SLOTS that exists; about 1,100 articles, 3 to 12 MB zipped, 2 to 4 s to fetch), not all 96: a full day is
about 0.5 GB and the whole history over a terabyte. That is plenty for a monthly decision (a month holds about 900 stock-market articles) and the history runs in
about 4 hours. The distilled file is kept under data/raw/News/gdelt, so a rerun reads nothing twice.

Data from 2015-02-19 (GDELT 2.0). Terms: unrestricted use including commercial, with a citation of the GDELT Project (https://www.gdeltproject.org/) in anything
published; licence row "GDELT". `known` is the day itself and the dataset declares a lag of one day: a day's file is stamped within that day.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from typing import Any

import pandas as pd

from quant.core.paths import REPO
from quant.data.sources.base import DataSource, Dataset, PullRequest
from quant.data.sources.registry import register

BASE = "http://data.gdeltproject.org/gdeltv2"
CACHE = REPO / "data/raw/News/gdelt"
THEMES = ("ECON_STOCKMARKET", "ECON_INFLATION", "ECON_INTEREST_RATES", "ECON_CENTRALBANK", "ECON_OILPRICE", "ECON_DEBT", "ECON_HOUSING_PRICES", "ECON_BANKRUPTCY",
          "UNEMPLOYMENT", "EPU_UNCERTAINTY", "EPU_CATS_REGULATION", "CYBER_ATTACK")
SLOTS = ("120000", "121500", "123000", "124500")            # shortcut: one file a day, the first that exists; read more slots for a finer daily figure
MAX_BYTES = 300 << 20                                        # a file that unpacks to more than this is refused
THEME_COLUMNS = {c: t for th in THEMES for c, t in ((f"n_{th.lower()}", "int64"), (f"tone_{th.lower()}", "float64"))}


def get(url: str) -> bytes:
    """The body of one GET request (a timeout or an HTTP error raises; nothing is retried)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "quant-research"}), timeout=120) as r:
        body: bytes = r.read()
    return body


def distil(zipped: bytes) -> dict[str, Any]:
    """One GKG 2.1 file as {n: articles, tone: sum of their tones, themes: {theme: [articles, sum of tones]}}. An article is a tab-separated line with a tone in
    field 15 (the first of its comma-separated numbers: positive minus negative); a line without one is skipped."""
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        info = z.infolist()[0]
        if info.file_size > MAX_BYTES:
            raise ValueError(f"{info.filename} unpacks to {info.file_size} bytes, above the {MAX_BYTES} limit")
        raw = z.read(info).decode("utf-8", errors="replace")
    n, tone, themes = 0, 0.0, {t: [0, 0.0] for t in THEMES}
    for line in raw.splitlines():
        f = line.split("\t")
        if len(f) < 16:
            continue
        try:
            t = float(f[15].split(",")[0])
        except ValueError:
            continue
        n += 1
        tone += t
        for th in set(THEMES) & set(f[7].split(";")):
            themes[th][0] += 1
            themes[th][1] += t
    return {"n": n, "tone": tone, "themes": themes}


def day_stats(day: dt.date) -> dict[str, Any] | None:
    """The distilled file of `day`: the first of SLOTS that exists, from the cache when read before; None when none exists (GDELT 2.0 starts 2015-02-19, and
    has gaps). An HTTP error other than 404 is raised; files already read stay cached."""
    for slot in SLOTS:
        stamp = f"{day:%Y%m%d}{slot}"
        f = CACHE / f"{stamp}.json"
        if f.exists():
            found = json.loads(f.read_text(encoding="utf-8"))
        else:
            try:
                found = distil(get(f"{BASE}/{stamp}.gkg.csv.zip"))
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                found = {}
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(found), encoding="utf-8")
            os.replace(tmp, f)                               # whole or absent: a pull killed mid-write leaves no half-written file
        if found:
            return dict(found)
    return None


def today() -> dt.date:
    return dt.date.today()


@register
class GdeltThemes(DataSource):
    dataset = Dataset(name="gdelt_themes", licence="GDELT", entity="market", observed="date", known="published",
                      columns={"n_articles": "int64", "tone_all": "float64", **THEME_COLUMNS}, lag_days=1,
                      description="GDELT news per day: articles and average tone, overall and for 12 economic themes (one 15-minute file a day)")

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        last = min(request.end, today() - dt.timedelta(days=1))          # today's files are not all there yet
        rows = []
        for day in pd.date_range(request.start, last):
            s = day_stats(day.date())
            if s is None:
                continue
            row: dict[str, Any] = {"market": "gdelt", "date": day, "published": day, "n_articles": s["n"], "tone_all": s["tone"] / s["n"] if s["n"] else float("nan")}
            for th, (n, tone) in s["themes"].items():
                row[f"n_{th.lower()}"] = n
                row[f"tone_{th.lower()}"] = tone / n if n else float("nan")
            rows.append(row)
        return pd.DataFrame(rows, columns=["market", "date", "published", "n_articles", "tone_all", *THEME_COLUMNS])
