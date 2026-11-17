"""A minimal stdlib .xlsx writer that supports formulas (the reader in xlsx.py has no formula support and openpyxl is not in the core lock).

Cells: str (text), int/float (number), datetime.date (date, mm/dd/yyyy), Formula(text without the leading '='), Bold(text). None is an empty cell.
"""
from __future__ import annotations

import datetime as dt
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

EPOCH = dt.date(1899, 12, 30)


@dataclass(frozen=True)
class Formula:
    """A formula cell; `text` without the leading '='."""
    text: str


@dataclass(frozen=True)
class Bold:
    """A text cell in bold."""
    text: str


Cell = str | int | float | dt.date | Formula | Bold | None
Row = Sequence[Cell]
Sheet = tuple[str, Sequence[Row], Sequence[float] | None, tuple[int, int] | None]       # (name, rows, column widths, freeze (rows, cols))


def col_letter(i: int) -> str:
    """The spreadsheet column letters of 0-based column `i` (0 -> A, 26 -> AA)."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, v: Cell) -> str:
    if v is None:
        return ""
    if isinstance(v, Formula):
        return f'<c r="{ref}"><f>{escape(v.text)}</f></c>'
    if isinstance(v, Bold):
        return f'<c r="{ref}" s="1" t="inlineStr"><is><t xml:space="preserve">{escape(v.text)}</t></is></c>'
    if isinstance(v, dt.date):
        return f'<c r="{ref}" s="2"><v>{(v - EPOCH).days}</v></c>'
    if isinstance(v, bool):
        raise TypeError("write booleans as text")
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"><v>{v!r}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'


def _sheet(rows: Sequence[Row], widths: Sequence[float] | None, freeze: tuple[int, int] | None) -> str:
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths or []))
    pane = ""
    if freeze:
        r, c = freeze
        pane = (f'<sheetViews><sheetView workbookViewId="0"><pane xSplit="{c}" ySplit="{r}" topLeftCell="{col_letter(c)}{r + 1}" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>')
    body = "".join(f'<row r="{r + 1}">' + "".join(_cell(f"{col_letter(c)}{r + 1}", v) for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{pane}{"<cols>" + cols + "</cols>" if cols else ""}<sheetData>{body}</sheetData></worksheet>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<numFmts count="1"><numFmt numFmtId="164" formatCode="mm/dd/yyyy"/></numFmts>'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
          '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
          '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>')


def write_workbook(path: Path, sheets: list[Sheet]) -> None:
    """`sheets`: (name, rows, column widths, freeze (rows, cols)). Names are at most 31 characters and avoid []:*?/\\ ."""
    path = Path(path)
    for name, *_ in sheets:
        if len(name) > 31 or any(ch in name for ch in "[]:*?/\\"):
            raise ValueError(f"bad sheet name {name!r}")
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(sheets)))
          + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (n, *_) in enumerate(sheets)) + "</sheets></workbook>")
    wrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
             + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wrels)
        z.writestr("xl/styles.xml", STYLES)
        for i, (_, rows, widths, freeze) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet(rows, widths, freeze))
