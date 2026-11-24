"""The GDELT themes source: a GKG file is distilled to article counts and average tones (overall and per theme), one file a day, kept so it is read once."""
from __future__ import annotations

import datetime as dt
import io
import urllib.error
import zipfile

import pandas as pd
import pytest

from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest
from quant.data.sources.plugins import gdelt_themes as G

DAY = dt.date(2026, 3, 2)
TODAY = dt.date(2026, 11, 18)


def article(themes: str, tone: str) -> str:
    """One GKG 2.1 line: 27 tab-separated fields, V1 themes in field 7 and the tone block in field 15."""
    f = [""] * 27
    f[7], f[15] = themes, tone
    return "\t".join(f)


def gkg(*lines: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", "\n".join(lines))
    return buf.getvalue()


@pytest.fixture
def gdelt(monkeypatch, tmp_path):
    """GDELT behind `get`: files answers {file stamp: zipped bytes}; a stamp mapped to an int raises that HTTP error, one not listed is a 404; the cache is in tmp_path."""
    state = type("State", (), {"files": {}, "asked": []})()

    def get(url):
        stamp = url.rsplit("/", 1)[1].removesuffix(".gkg.csv.zip")
        state.asked.append(stamp)
        body = state.files.get(stamp, 404)
        if isinstance(body, int):
            raise urllib.error.HTTPError(url, body, "error", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return body

    monkeypatch.setattr(G, "get", get)
    monkeypatch.setattr(G, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(G, "today", lambda: TODAY)
    return state


def test_a_file_is_distilled_to_article_counts_and_tones_overall_and_per_theme():
    s = G.distil(gkg(article("ECON_INFLATION;TAX_FNCACT;", "-2.0,1.0,3.0,4.0,0,0,100"), article("ECON_INFLATION;ECON_DEBT;", "4.0,5.0,1.0,6.0,0,0,50"),
                     article("EDUCATION;", "1.0,2.0,1.0,3.0,0,0,10"), "too short", article("ECON_DEBT;", "n/a,1,1,1")))
    assert s["n"] == 3 and s["tone"] == 3.0                                    # the short line and the line without a number are not articles
    assert s["themes"]["ECON_INFLATION"] == [2, 2.0] and s["themes"]["ECON_DEBT"] == [1, 4.0] and s["themes"]["ECON_STOCKMARKET"] == [0, 0.0]


def test_a_file_unpacking_above_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(G, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="above the 10 limit"):
        G.distil(gkg(article("ECON_DEBT;", "1.0,1,1,1")))


def test_a_day_is_one_row_of_means_and_a_theme_nobody_mentioned_has_no_tone(gdelt, tmp_path):
    gdelt.files["20260302120000"] = gkg(article("ECON_INFLATION;", "-2.0,0,0,0"), article("ECON_INFLATION;", "4.0,0,0,0"), article("OTHER;", "1.0,0,0,0"))
    m = ST.pull(G.GdeltThemes(), PullRequest(DAY, DAY), root=tmp_path / "store", today=TODAY)
    row = ST.load(G.GdeltThemes(), root=tmp_path / "store").iloc[0]
    assert m["rows"] == 1 and row["n_articles"] == 3 and row["tone_all"] == pytest.approx(1.0)
    assert row["n_econ_inflation"] == 2 and row["tone_econ_inflation"] == pytest.approx(1.0)
    assert row["n_econ_debt"] == 0 and pd.isna(row["tone_econ_debt"])


def test_the_next_slot_is_used_when_the_first_file_of_the_day_is_missing_and_a_day_without_any_has_no_row(gdelt):
    gdelt.files["20260302121500"] = gkg(article("ECON_DEBT;", "1.0,0,0,0"))
    out = G.GdeltThemes().fetch(PullRequest(DAY, DAY + dt.timedelta(days=1)))
    assert out["date"].dt.date.tolist() == [DAY] and gdelt.asked == ["20260302120000", "20260302121500", *[f"20260303{s}" for s in G.SLOTS]]


def test_a_file_read_once_is_not_downloaded_again_and_a_missing_one_is_not_asked_for_twice(gdelt):
    gdelt.files["20260302121500"] = gkg(article("ECON_DEBT;", "1.0,0,0,0"))
    G.day_stats(DAY)
    asked = len(gdelt.asked)
    assert G.day_stats(DAY)["n"] == 1 and len(gdelt.asked) == asked


def test_a_server_error_stops_the_pull_and_caches_nothing_for_that_file(gdelt):
    gdelt.files["20260302120000"] = 503
    with pytest.raises(urllib.error.HTTPError):
        G.day_stats(DAY)
    assert not (G.CACHE / "20260302120000.json").exists()


def test_today_is_never_read_because_its_files_are_not_all_there_yet(gdelt):
    assert G.GdeltThemes().fetch(PullRequest(TODAY, TODAY)).empty and gdelt.asked == []


def test_a_write_that_fails_part_way_leaves_no_cache_file_so_the_day_is_read_again(gdelt, monkeypatch):
    gdelt.files["20260302120000"] = gkg(article("ECON_DEBT;", "1.0,0,0,0"))

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(G.os, "replace", broken)
    with pytest.raises(OSError):
        G.day_stats(DAY)
    assert not (G.CACHE / "20260302120000.json").exists()
