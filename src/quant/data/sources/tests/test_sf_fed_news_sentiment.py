"""The SF Fed news sentiment source: the Data sheet of the Fed's spreadsheet read to one figure a day."""
from __future__ import annotations

import datetime as dt
import io
import zipfile

import pytest

from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest
from quant.data.sources.plugins import sf_fed_news_sentiment as S

TODAY = dt.date(2026, 11, 18)
REQ = PullRequest(dt.date(2020, 1, 1), dt.date(2026, 11, 17))
HEAD = '<c r="A1" t="s"><v>1606</v></c><c r="B1" t="s"><v>1607</v></c>'
SHEET_XML = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{}</sheetData></worksheet>'


def workbook(*days: tuple[int, float | None]) -> bytes:
    """A spreadsheet whose Data sheet has the header row and one row per (Excel serial date, figure); a None figure leaves the row with a single cell."""
    rows = [f"<row>{HEAD}</row>"]
    for serial, value in days:
        cells = f'<c r="A{len(rows) + 1}"><v>{serial}</v></c>' + (f'<c r="B{len(rows) + 1}"><v>{value}</v></c>' if value is not None else "")
        rows.append(f"<row>{cells}</row>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(S.SHEET, SHEET_XML.format("".join(rows)))
    return buf.getvalue()


def serial(day: dt.date) -> int:
    return (day - S.EXCEL_DAY0).days


def test_each_row_is_a_day_and_its_figure_and_the_header_and_incomplete_rows_are_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "get", lambda url: workbook((serial(dt.date(2026, 11, 2)), -0.25), (serial(dt.date(2026, 11, 3)), None), (serial(dt.date(2026, 11, 4)), 0.5)))
    m = ST.pull(S.SfFedNewsSentiment(), REQ, root=tmp_path, today=TODAY)
    d = ST.load(S.SfFedNewsSentiment(), root=tmp_path)
    assert m["rows"] == 2 and d["date"].dt.date.tolist() == [dt.date(2026, 11, 2), dt.date(2026, 11, 4)] and d["sentiment"].tolist() == [-0.25, 0.5]


def test_only_days_inside_the_request_are_returned(monkeypatch):
    monkeypatch.setattr(S, "get", lambda url: workbook(*[(serial(dt.date(2026, 11, d)), 0.1) for d in (1, 2, 3)]))
    out = S.SfFedNewsSentiment().fetch(PullRequest(dt.date(2026, 11, 2), dt.date(2026, 11, 2)))
    assert out["date"].dt.date.tolist() == [dt.date(2026, 11, 2)]


def test_a_sheet_unpacking_above_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(S, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="above the 10 limit"):
        S.read_sheet(workbook((serial(dt.date(2026, 11, 2)), 0.1)))


def test_a_spreadsheet_without_data_rows_stops_the_pull(monkeypatch):
    monkeypatch.setattr(S, "get", lambda url: workbook())
    with pytest.raises(ValueError, match="no data rows"):
        S.SfFedNewsSentiment().fetch(REQ)
