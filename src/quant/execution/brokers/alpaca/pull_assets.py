"""Read-only pull of the Alpaca paper account's active US equity assets (PAPER amendment A10), stored for quant.data.instruments.alpaca.

    dotenvx run -- python -m quant.execution.brokers.alpaca.pull_assets     # -> data/raw/Alpaca/alpaca_<date>/assets.json
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from quant.execution.brokers.alpaca import api
from quant.data.instruments.alpaca import COLUMNS, assets_file


def pull(today: dt.date | None = None) -> Path:
    rows = api.get("/v2/assets?status=active&asset_class=us_equity")
    keep = [{k: r.get(k) for k in COLUMNS} for r in rows]
    f = assets_file((today or dt.date.today()).isoformat())
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(keep, indent=0) + "\n", encoding="utf-8")
    return f


if __name__ == "__main__":
    print(pull())
