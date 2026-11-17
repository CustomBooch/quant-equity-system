"""quant.data.sec.sec_pull: the EDGAR form-index parser (the defect it replaces cut every filing date to the first of its month) and the download bookkeeping, with HTTP replaced."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from quant.data.sec import sec_pull as SP

HEADER = """Description:           Master Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    October 23, 2026

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
"""


def idx_line(form: str, name: str, cik: int, date: str, path: str) -> str:
    return f"{form:<12}{name:<62}{cik:<12}{date:<12}{path}"


class TestParseFormIdx:
    def test_every_filing_keeps_its_full_date_even_after_a_company_name_longer_than_its_column(self):
        text = HEADER + "\n".join([
            idx_line("8-K", "SHORT CO", 1877461, "2026-09-16", "edgar/data/1877461/0001477932-26-006027.txt"),
            "8-K         A COMPANY NAME SO LONG THAT IT RUNS PAST THE COLUMN WIDTH OF THE INDEX FILE INC   1002638     2026-09-30  edgar/data/1002638/0001002638-26-000087.txt",
        ])
        d = SP.parse_form_idx(text)
        assert d["filingDate"].tolist() == [pd.Timestamp("2026-09-16"), pd.Timestamp("2026-09-30")]
        assert d["cik"].tolist() == [1877461, 1002638]
        assert d["path"].str.startswith("edgar/").all()

    def test_a_form_type_with_spaces_is_read_whole(self):
        d = SP.parse_form_idx(HEADER + idx_line("SC 13D/A", "HOLDER LP", 42, "2026-07-01", "edgar/data/42/0000000042-26-000001.txt"))
        assert d["form"].tolist() == ["SC 13D/A"]

    def test_lines_that_are_not_filings_are_skipped(self):
        d = SP.parse_form_idx(HEADER + "\n" + "a trailing note without the four fields\n")
        assert d.empty


class TestDownloads:
    @pytest.fixture()
    def out(self, tmp_path, monkeypatch):
        monkeypatch.setattr(SP, "OUT", tmp_path / "sec")
        monkeypatch.setattr(SP.time, "sleep", lambda s: None)
        monkeypatch.setenv("SEC_CONTACT", "tester")
        return tmp_path / "sec"

    def test_a_downloaded_file_is_recorded_in_the_manifest_and_never_fetched_again(self, out, monkeypatch):
        calls = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"zipbytes"
        monkeypatch.setattr(SP.urllib.request, "urlopen", lambda req, timeout: calls.append(req.full_url) or Resp())
        SP.fetch("ftd/a.zip", "https://www.sec.gov/a.zip")
        SP.fetch("ftd/a.zip", "https://www.sec.gov/a.zip")
        assert calls == ["https://www.sec.gov/a.zip"]
        m = json.loads((out / "MANIFEST.json").read_text())["files"]["ftd/a.zip"]
        assert m["bytes"] == 8 and len(m["sha256"]) == 64

    def test_a_company_whose_item_lookup_fails_is_recorded_and_the_others_are_kept(self, out, tmp_path, monkeypatch):
        subs = tmp_path / "subs.parquet"
        pd.DataFrame({"filingDate": pd.to_datetime(["2026-09-14"])}).to_parquet(subs)
        monkeypatch.setattr(SP, "SUBS", subs)
        out.mkdir()
        pd.DataFrame({"form": ["8-K", "8-K", "8-K"], "kind": ["8k"] * 3, "cik": [1, 2, 3], "filingDate": pd.to_datetime(["2026-09-20", "2026-09-21", "2026-09-10"]),
                      "accession": ["x-1", "y-1", "z-1"]}).to_parquet(out / "index.parquet")
        asked = []

        def fake_get(url):
            asked.append(url)
            if "CIK0000000002" in url:
                raise OSError("connection reset")
            return json.dumps({"filings": {"recent": {"accessionNumber": ["x-1", "x-0"], "form": ["8-K", "8-K"], "filingDate": ["2026-09-20", "2026-09-01"],
                                                      "items": ["5.02,9.01", "2.02"]}}}).encode()
        monkeypatch.setattr(SP, "get", fake_get)
        SP.items()
        assert len(asked) == 2                                                      # CIK 3 filed before the extract ends: not asked
        got = pd.read_parquet(out / "items.parquet")
        assert got["accession"].tolist() == ["x-1"] and got["items"].tolist() == ["5.02,9.01"]
        err = json.loads((out / "items_errors.json").read_text())
        assert err["errors"][0]["cik"] == 2 and "connection reset" in err["errors"][0]["error"]

    def test_items_an_earlier_pull_holds_are_reused_and_filings_after_the_through_date_are_not_asked_for(self, out, tmp_path, monkeypatch):
        subs = tmp_path / "subs.parquet"
        pd.DataFrame({"filingDate": pd.to_datetime(["2026-09-14"])}).to_parquet(subs)
        monkeypatch.setattr(SP, "SUBS", subs)
        monkeypatch.setattr(SP, "REPO", tmp_path)
        (tmp_path / "data/raw_ext/old").mkdir(parents=True)
        pd.DataFrame({"accession": ["x-1"], "form": ["8-K"], "filingDate": pd.to_datetime(["2026-09-20"]), "items": ["5.02"], "cik": [1]}).to_parquet(tmp_path / "data/raw_ext/old/items.parquet")
        out.mkdir()
        pd.DataFrame({"form": ["8-K"] * 3, "kind": ["8k"] * 3, "cik": [1, 2, 3], "filingDate": pd.to_datetime(["2026-09-20", "2026-10-20", "2026-11-05"]),
                      "accession": ["x-1", "y-1", "z-1"]}).to_parquet(out / "index.parquet")
        monkeypatch.setenv("SEC_PULL_REUSE", "old")
        monkeypatch.setenv("SEC_PULL_THROUGH", "2026-10-30")
        asked = []

        def fake_get(url):
            asked.append(url)
            return json.dumps({"filings": {"recent": {"accessionNumber": ["y-1"], "form": ["8-K"], "filingDate": ["2026-10-20"], "items": ["2.02"]}}}).encode()
        monkeypatch.setattr(SP, "get", fake_get)
        SP.items()
        assert asked == ["https://data.sec.gov/submissions/CIK0000000002.json"]      # CIK 1 is held already; CIK 3 filed after the through date
        got = pd.read_parquet(out / "items.parquet").sort_values("accession")
        assert got["accession"].tolist() == ["x-1", "y-1"] and json.loads((out / "items_errors.json").read_text())["reused_rows"] == 1

    def test_no_request_is_made_without_a_contact_string(self, out, monkeypatch):
        monkeypatch.delenv("SEC_CONTACT")
        with pytest.raises(SystemExit, match="set SEC_CONTACT"):
            SP.get("https://www.sec.gov/x")
