from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

import duckdb
import pandas as pd
import pytest

from quant.data.capiq.request_workbook import FIELDS, SNAPSHOTS, build, cusip9
from quant.core.xlsx_write import Formula, col_letter, write_workbook

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.mark.parametrize("c8,full", [("03783310", "037833100"), ("59491810", "594918104"), ("38259P50", "38259P508")])
def test_cusip_check_digit_matches_known_cusips(c8, full):
    assert cusip9(c8) == full


def test_a_missing_or_short_cusip_has_no_check_digit():
    assert cusip9(None) is None and cusip9("123") is None


def test_column_letters_run_a_to_z_then_aa_to_zz_then_aaa():
    assert [col_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]


def cells(path, sheet_no):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet_no}.xml"))
    out = {}
    for c in root.iterfind(".//m:c", NS):
        f = c.find("m:f", NS)
        t = c.find("m:is/m:t", NS)
        v = c.find("m:v", NS)
        out[c.get("r")] = ("f", f.text) if f is not None else (("s", t.text) if t is not None else (("n", v.text) if v is not None else None))
    return out


def fixtures(tmp_path, n=40):
    panel, state = tmp_path / "p.parquet", tmp_path / "s.parquet"
    con = duckdb.connect()
    st = pd.DataFrame({"permno": range(1, n + 1), "ticker": [f"T{i}" for i in range(n)], "cusip": [f"0378331{i % 10}0" if i != 3 else None for i in range(n)],
                       "exch": "N", "me": [float(n - i) for i in range(n)], "smonth": pd.Timestamp("2026-06-01").date()})
    pn = pd.DataFrame({"permno": list(range(1, n + 1)) * 2, "month": [pd.Timestamp("2026-06-01").date()] * n + [pd.Timestamp("2026-10-01").date()] * n,
                       "in_investable": [i % 4 != 0 for i in range(n)] + [False] * n})
    con.register("st", st), con.register("pn", pn)
    con.execute(f"copy st to '{state.as_posix()}' (format parquet)"), con.execute(f"copy pn to '{panel.as_posix()}' (format parquet)")
    return panel, state


def test_workbook_has_the_universe_formulas_and_consistent_references(tmp_path):
    panel, state = fixtures(tmp_path)
    out = tmp_path / "req.xlsx"
    info = build(panel, state, out)
    assert info["stocks"] == 30 and info["missing_cusip"] >= 0                                  # 10 of 40 are not investable (every fourth)
    assert info["sheets"] == ["README", "Config", "Universe", "Pilot_2026-06-18"] + [f"S_{d}" for d, _ in SNAPSHOTS]
    uni = cells(out, 3)
    assert uni["A1"] == ("s", "Identifier") and uni["B2"][0] == "s" and uni["A2"][0] == "f" and "Config!$B$3" in uni["A2"][1] and uni["D2"][1] is not None
    assert [uni[f"F{r}"] for r in (2, 3)] == [("n", "2"), ("n", "3")]                                # permno 1 is not investable; biggest market equity first
    cfg = cells(out, 2)
    snap = cells(out, 5)                                                                          # the first snapshot sheet is the Pilot (sheet 4) then S_ (sheet 5)
    pilot = cells(out, 4)
    for j in range(len(FIELDS)):
        col = col_letter(3 + j)
        ref = re.fullmatch(r"Config!B(\d+)", pilot[f"{col}3"][1])
        assert cfg[f"B{ref.group(1)}"] == ("s", FIELDS[j][1]) and cfg[f"A{ref.group(1)}"] == ("s", FIELDS[j][0])
        pref = re.fullmatch(r"Config!C(\d+)", pilot[f"{col}4"][1])
        assert cfg[f"C{pref.group(1)}"] == ("s", FIELDS[j][2])
    assert pilot["D6"][1] == 'CIQ($A6,D$3,D$4,D$5)' and pilot["A6"][1] == "Universe!A2" and pilot["C35"][1] == "Universe!D31" and "A36" not in pilot       # 30 pilot rows
    assert snap["D6"][0] == "f" and sum(1 for k, v in snap.items() if v and v[0] == "f" and re.fullmatch(r"[D-N]\d+", k) and int(k[1:]) >= 6) == 30 * len(FIELDS)
    assert pilot["D5"][0] == "n" and int(pilot["D5"][1]) == (pd.Timestamp("2026-06-18") - pd.Timestamp("1899-12-30")).days


def test_formula_template_override_is_applied_everywhere(tmp_path):
    panel, state = fixtures(tmp_path)
    out = tmp_path / "req2.xlsx"
    build(panel, state, out, '=CIQ({id},{mnem},{period},,,,,{asof})')
    pilot = cells(out, 4)
    assert pilot["E7"][1] == "CIQ($A7,E$3,E$4,,,,,E$5)"


def test_a_sheet_name_excel_cannot_hold_is_refused(tmp_path):
    with pytest.raises(ValueError):
        write_workbook(tmp_path / "x.xlsx", [("bad/name", [[Formula("1+1")]], None, None)])
