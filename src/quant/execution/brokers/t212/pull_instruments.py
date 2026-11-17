"""Read-only pull of the Trading 212 instrument list (GET /equity/metadata/instruments on the practice account, read-only keys; one request, no trading endpoint).
python -m quant.execution.brokers.t212.pull_instruments -> data/raw_ext/t212_<date>/instruments.json (the raw body, as received)"""
from __future__ import annotations

import datetime as dt
import json

from quant.execution.brokers.t212.api import get_bytes
from quant.data.instruments.t212 import instruments_file


def main() -> None:
    body = get_bytes("/equity/metadata/instruments")
    out = instruments_file(dt.date.today().isoformat())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    data = json.loads(body)
    print("instruments", len(data), "bytes", len(body), "fields:", sorted(data[0]) if data else None)


if __name__ == "__main__":
    main()
