"""quant.core.xlsx: every sheet of a workbook is read with shared, rich-text and inline strings resolved, and a malformed part is refused by name."""
from __future__ import annotations

import zipfile

import pytest

from quant.core.xlsx import XlsxFormatError, read_sheets

WB = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
      '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
RELS = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/></Relationships>')


def workbook(path, sheet_data, wb=WB, shared=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", RELS)
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{sheet_data}</sheetData></worksheet>')
    return path


def test_shared_rich_and_inline_strings_numbers_and_empty_cells_are_read_by_row_and_column(tmp_path):
    f = workbook(tmp_path / "a.xlsx", '<row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="s"><v>1</v></c><c r="AB2"><v>4.25</v></c>'
                                      '<c r="C2"/><c r="D2" t="inlineStr"><is><t>inline</t></is></c></row>',
                 shared="<si><t>Entity Name</t></si><si><r><t>Rich </t></r><r><t>text</t></r></si>")
    assert read_sheets(f) == {"Sheet1": {2: {"A": "Entity Name", "B": "Rich text", "AB": "4.25", "C": None, "D": "inline"}}}


def test_a_workbook_without_a_sheet_list_is_refused(tmp_path):
    f = workbook(tmp_path / "b.xlsx", "", wb='<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
    with pytest.raises(XlsxFormatError, match="no workbook sheets element"):
        read_sheets(f)


def test_a_cell_whose_reference_has_no_column_letters_is_refused(tmp_path):
    f = workbook(tmp_path / "c.xlsx", '<row r="1"><c r="12"><v>1</v></c></row>')
    with pytest.raises(XlsxFormatError, match="no column letters"):
        read_sheets(f)


def test_a_part_that_would_unpack_above_the_size_limit_is_refused_before_it_is_read(tmp_path, monkeypatch):
    f = workbook(tmp_path / "big.xlsx", '<row r="1"><c r="A1"><v>1</v></c></row>')
    monkeypatch.setattr("quant.core.xlsx.MAX_PART_BYTES", 50)
    with pytest.raises(XlsxFormatError, match=r"part xl/.* unpacks to \d+ bytes, above the 50 limit"):
        read_sheets(f)


def test_a_workbook_listing_more_sheets_than_the_limit_is_refused(tmp_path, monkeypatch):
    f = workbook(tmp_path / "many.xlsx", '<row r="1"><c r="A1"><v>1</v></c></row>')
    assert list(read_sheets(f))                                                         # one sheet is fine
    monkeypatch.setattr("quant.core.xlsx.MAX_SHEETS", 0)
    with pytest.raises(XlsxFormatError, match="lists 1 sheets, above the 0 limit"):
        read_sheets(f)
