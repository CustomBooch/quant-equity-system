"""Monthly news-based indices, already distilled by their authors (`news_indices`): the Economic Policy Uncertainty index (Baker, Bloom and Davis: the share of
newspaper articles about economic policy and uncertainty) and the Equity Market Volatility tracker (the share of articles that link the stock market, volatility
and a topic), the overall US index and the monetary, trade and financial-regulation categories, and the EMV topics inflation, interest rates, financial crises,
monetary policy, trade policy and commodity markets. Read from the FRED API (a free key in FRED_API_KEY); 1985 on.

`known` is the last day of the month and the dataset declares a lag of 45 days: the US index and the EMV figures appear in the first days of the next month, the
categorical EPU indices a month later. The values are the latest ones FRED holds, and the authors rescale and revise past months, so a backtest sees slightly better
data than was available then: use these as a monthly state variable, not as a precise historical signal. Terms: free to use citing the authors
(https://www.policyuncertainty.com); the licence row "News-based indices (EPU, EMV)" does not confirm real-money use.
"""
from __future__ import annotations

import json
import os
import urllib.parse

import pandas as pd

from quant.data.sources.base import DataSource, Dataset, PullRequest
from quant.data.sources.base import http_get as get
from quant.data.sources.registry import register

URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES = {"epu_us": "USEPUINDXM", "epu_monetary": "EPUMONETARY", "epu_trade": "EPUTRADE", "epu_finreg": "EPUFINREG",
          "emv_overall": "EMVOVERALLEMV", "emv_inflation": "EMVMACROINFLATION", "emv_interest_rates": "EMVMACROINTEREST", "emv_fin_crises": "EMVFINCRISES",
          "emv_monetary": "EMVMONETARYPOL", "emv_trade": "EMVTRADEPOLEMV", "emv_commodities": "EMVCOMMMKT"}


@register
class NewsIndices(DataSource):
    dataset = Dataset(name="news_indices", licence="News-based indices (EPU, EMV)", entity="market", observed="month", known="published",
                      columns={c: "float64" for c in SERIES}, lag_days=45,
                      description="Monthly newspaper-based policy uncertainty (EPU) and equity market volatility (EMV) indices, from FRED")

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        key = os.environ.get("FRED_API_KEY")
        if not key:
            raise RuntimeError("news_indices: FRED_API_KEY is not set (a free key: https://fred.stlouisfed.org/docs/api/api_key.html)")
        cols = {}
        for name, fred_id in SERIES.items():
            obs = json.loads(get(f"{URL}?{urllib.parse.urlencode({'series_id': fred_id, 'api_key': key, 'file_type': 'json'})}"))["observations"]
            cols[name] = pd.Series([float(o["value"]) if o["value"] != "." else float("nan") for o in obs], index=pd.to_datetime([o["date"] for o in obs]))
        d = pd.DataFrame(cols).dropna(how="all")
        d.index.name = "month"
        d = d.reset_index()
        d["published"] = d["month"] + pd.offsets.MonthEnd(0)
        d["market"] = "news"
        d = d[(d["published"] >= pd.Timestamp(request.start)) & (d["published"] <= pd.Timestamp(request.end))]
        return d[["market", "month", "published", *SERIES]]
