"""Minimal stdlib .xlsx reader (no openpyxl): every sheet as {row number: {column letter: value}}. Values are str or None; shared and inline strings are resolved.

The XML parts are parsed by the Rust kernel (quant.core.kernels.xlsx_rows / xlsx_shared, same result, tested on every Capital IQ export) when it is built, and by
ElementTree otherwise or whenever the kernel declines a part (another encoding, an unexpected construct)."""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from quant.core import kernels as KN

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_M = "{%s}" % NS["m"]

Rows = dict[int, dict[str, str | None]]           # {row number: {column letter: cell text or None}} of one sheet


class XlsxFormatError(ValueError):
    """A part of the workbook is missing or not shaped as the reader expects."""


def _required(node: ET.Element | None, what: str) -> ET.Element:
    if node is None:
        raise XlsxFormatError(f"no {what} element")
    return node


def _attr(node: ET.Element, name: str) -> str:
    v = node.get(name)
    if v is None:
        raise XlsxFormatError(f"<{node.tag}> has no {name} attribute")
    return v


MAX_PART_BYTES = 1 << 30                                           # amendment A17: no part of a workbook may unpack to more than 1 GiB


def _part(z: zipfile.ZipFile, name: str) -> bytes:
    """One part of the archive, refused before it is unpacked when its declared size is above MAX_PART_BYTES (a small file can unpack to gigabytes)."""
    size = z.getinfo(name).file_size
    if size > MAX_PART_BYTES:
        raise XlsxFormatError(f"part {name} unpacks to {size} bytes, above the {MAX_PART_BYTES} limit")
    return z.read(name)


def read_sheets(path: Path | str) -> dict[str, Rows]:
    """Every sheet of the workbook by name. The archive is closed before returning; XlsxFormatError if a required part is malformed."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            raw = _part(z, "xl/sharedStrings.xml")
            from_kernel = KN.xlsx_shared(raw)
            shared = _shared_et(raw) if from_kernel is None else from_kernel
        wb = ET.fromstring(_part(z, "xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(_part(z, "xl/_rels/workbook.xml.rels"))}
        out: dict[str, Rows] = {}
        for sh in _required(wb.find("m:sheets", NS), "workbook sheets"):
            target = rels[_attr(sh, "{%s}id" % NS["r"])]
            if target is None:
                raise XlsxFormatError(f"sheet {sh.get('name')!r} has no relationship target")
            target = target.lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            raw = _part(z, target)
            rows = KN.xlsx_rows(raw, shared)
            out[_attr(sh, "name")] = _rows_et(raw, shared) if rows is None else rows
    return out


def _shared_et(raw: bytes) -> list[str]:
    """The ElementTree reference for the shared-strings part."""
    return ["".join(t.text or "" for t in si.iter(_M + "t")) for si in ET.fromstring(raw).findall("m:si", NS)]


def _rows_et(raw: bytes, shared: list[str]) -> Rows:
    """The ElementTree reference for one worksheet part."""
    rows: Rows = {}
    for row in _required(ET.fromstring(raw).find("m:sheetData", NS), "sheetData").findall("m:row", NS):
        cells: dict[str, str | None] = {}
        for c in row.findall("m:c", NS):
            ref = re.match(r"[A-Z]+", _attr(c, "r"))
            if ref is None:
                raise XlsxFormatError(f"cell reference {c.get('r')!r} has no column letters")
            col = ref.group(0)
            v = c.find("m:v", NS)
            val = None if v is None else v.text
            if c.get("t") == "s" and val is not None:
                val = shared[int(val)]
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(_M + "t"))
            cells[col] = val
        rows[int(_attr(row, "r"))] = cells
    return rows
