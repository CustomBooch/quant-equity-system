"""The Guardian counts source: one row per finished month of article counts, each count asked for once and kept."""
from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
import urllib.parse

import pytest

from quant.data.sources import store as ST
from quant.data.sources.base import PullRequest
from quant.data.sources.plugins import guardian_counts as G

TODAY = dt.date(2026, 11, 18)
START = dt.date(2026, 9, 1)


@pytest.fixture
def guardian(monkeypatch, tmp_path):
    """The Guardian behind `get`: state.answers is the JSON body to send ({} gives total 7), state.asked the parsed query of every request."""
    state = type("State", (), {"answers": None, "asked": []})()

    def get(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        state.asked.append(q)
        if state.answers is not None:
            return state.answers
        return json.dumps({"response": {"status": "ok", "total": 7}}).encode()

    monkeypatch.setattr(G, "get", get)
    monkeypatch.setattr(G, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(G, "today", lambda: TODAY)
    monkeypatch.setenv("GUARDIAN_API_KEY", "k")
    return state


def test_a_finished_month_is_one_row_with_a_count_per_series_and_an_unfinished_month_has_none(guardian, tmp_path):
    m = ST.pull(G.GuardianCounts(), PullRequest(START, TODAY), root=tmp_path / "store", today=TODAY)
    row = ST.load(G.GuardianCounts(), root=tmp_path / "store").iloc[0]
    assert m["rows"] == 2 and row["n_all"] == 7 and row["n_tariffs"] == 7 and str(row["published"].date()) == "2026-09-30"     # September and October; November is not over


def test_each_series_asks_for_its_own_section_or_phrase_over_the_whole_month(guardian):
    G.GuardianCounts().fetch(PullRequest(START, dt.date(2026, 9, 30)))
    asked = {tuple(sorted((k, v) for k, v in q.items() if k in ("section", "q"))): q for q in guardian.asked}
    assert len(guardian.asked) == len(G.SERIES) and asked[(("section", "business"),)]["from-date"] == "2026-09-01" and asked[(("section", "business"),)]["to-date"] == "2026-09-30"
    assert (("q", '"interest rates"'),) in asked and () in asked


def test_a_start_inside_a_month_begins_with_the_next_month(guardian):
    out = G.GuardianCounts().fetch(PullRequest(dt.date(2026, 9, 15), dt.date(2026, 11, 30)))
    assert [d.date() for d in out["month"]] == [dt.date(2026, 10, 1)]


def test_a_count_asked_for_once_is_not_asked_for_again(guardian):
    G.count("n_all", START, dt.date(2026, 9, 30))
    G.count("n_all", START, dt.date(2026, 9, 30))
    assert len(guardian.asked) == 1


def test_no_key_says_where_to_get_one(guardian, monkeypatch):
    monkeypatch.delenv("GUARDIAN_API_KEY")
    with pytest.raises(RuntimeError, match="GUARDIAN_API_KEY is not set"):
        G.count("n_all", START, dt.date(2026, 9, 30))


def test_an_answer_that_is_not_ok_stops_the_pull_and_caches_nothing(guardian):
    guardian.answers = json.dumps({"response": {"status": "error", "message": "bad key"}}).encode()
    with pytest.raises(RuntimeError, match="bad key"):
        G.count("n_all", START, dt.date(2026, 9, 30))
    assert not list(G.CACHE.glob("*.json"))


def test_a_quota_error_stops_the_pull(guardian, monkeypatch):
    def get(url):
        raise urllib.error.HTTPError(url, 429, "too many", {}, io.BytesIO(b""))  # type: ignore[arg-type]
    monkeypatch.setattr(G, "get", get)
    with pytest.raises(urllib.error.HTTPError):
        G.count("n_all", START, dt.date(2026, 9, 30))


def test_a_write_that_fails_part_way_leaves_no_cache_file(guardian, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(G.os, "replace", broken)
    with pytest.raises(OSError):
        G.count("n_all", START, dt.date(2026, 9, 30))
    assert not list(G.CACHE.glob("*.json"))
