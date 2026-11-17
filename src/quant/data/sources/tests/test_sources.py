"""quant.data.sources: a declaration is checked when it is registered; a pull is conformed, checked point in time and stored with a manifest; a stored pull
is read back newest first, checked against its hash, and filtered to what was usable on a date; every registered source names a licence row."""
from __future__ import annotations

import datetime as dt
import json
from typing import ClassVar

import pandas as pd
import pytest

from quant.core import licences as L
from quant.data.sources import registry as RG
from quant.data.sources import store as ST
from quant.data.sources.base import DataSource, Dataset, PullRequest, conform, validate
from quant.data.sources.files import FileSource

DS = Dataset(name="toy_quarterly", licence="Data source framework", entity="ticker", observed="period_end", known="published",
             columns={"sales": "float64", "fiscal_year": "int64"}, description="a test dataset")
REQ = PullRequest(dt.date(2025, 1, 1), dt.date(2026, 12, 31))
TODAY = dt.date(2026, 11, 17)


def rows(**over: list) -> pd.DataFrame:
    base = {"ticker": ["AAA", "AAA", "BBB"], "period_end": ["2026-03-31", "2026-06-30", "2026-06-30"], "published": ["2026-04-30", "2026-07-31", "2026-08-10"],
            "sales": [1.0, 2.0, 5.0], "fiscal_year": [2026, 2026, 2026], "unused": ["x", "y", "z"]}
    return pd.DataFrame({**base, **over})


class Toy(DataSource):
    dataset = DS
    frame: ClassVar[pd.DataFrame] = rows()

    def fetch(self, request: PullRequest) -> pd.DataFrame:
        return self.frame


def test_a_sound_declaration_has_no_problems_and_each_unsound_part_is_named():
    assert DS.problems() == []
    bad = Dataset(name="Bad-Name", licence="x", entity="isin", observed="d", known="d", columns={"d": "float64", "v": "decimal"})
    p = " | ".join(bad.problems())
    for words in ("lower_snake_case", "entity 'isin'", "column types", "repeat a key column", "no lag"):
        assert words in p


def test_registering_an_unsound_declaration_or_a_second_source_of_the_same_name_is_refused():
    class Unsound(DataSource):
        dataset = Dataset(name="unsound", licence="x", entity="ticker", observed="d", known="k", columns={})
    with pytest.raises(ValueError, match="no value columns"):
        RG.register(Unsound)
    RG.register(Toy)
    RG.register(Toy)                                                    # the same class again is fine

    class Other(Toy):
        pass
    with pytest.raises(ValueError, match="already registered"):
        RG.register(Other)


def test_conform_keeps_the_declared_columns_in_order_with_their_types_and_names_a_missing_one():
    f = conform(rows(), DS)
    assert list(f.columns) == ["ticker", "period_end", "published", "sales", "fiscal_year"]
    assert str(f["published"].dtype).startswith("datetime64") and f["fiscal_year"].dtype == "int64"
    with pytest.raises(KeyError, match="sales"):
        conform(rows().drop(columns="sales"), DS)


def test_the_point_in_time_checks_catch_early_future_missing_and_duplicate_rows():
    assert validate(conform(rows(), DS), DS, TODAY) == []
    early = conform(rows(published=["2026-03-01", "2026-07-31", "2026-08-10"]), DS)
    assert "known before the date they describe" in validate(early, DS, TODAY)[0]
    future = conform(rows(published=["2026-04-30", "2026-07-31", "2026-12-01"]), DS)
    assert "after today" in validate(future, DS, TODAY)[0]
    dup = conform(pd.concat([rows(), rows().iloc[[0]]]), DS)
    assert "duplicate" in validate(dup, DS, TODAY)[0]


def test_a_pull_is_stored_with_its_manifest_and_read_back_checked_and_as_of_a_date(tmp_path):
    m = ST.pull(Toy(), REQ, tmp_path, TODAY)
    assert m["rows"] == 3 and m["entities"] == 2 and m["known"] == ["2026-04-30", "2026-08-10"]
    stored = json.loads((tmp_path / "toy_quarterly" / "2026-11-17" / "MANIFEST.json").read_text())
    assert stored["sha256"] == m["sha256"] and stored["licence"] == "Data source framework"
    assert len(ST.load(Toy(), root=tmp_path)) == 3
    assert ST.load(Toy(), as_of=dt.date(2026, 8, 1), root=tmp_path)["sales"].tolist() == [1.0, 2.0]          # BBB was published on 2026-08-10


def test_a_pull_that_fails_its_checks_or_is_empty_writes_nothing(tmp_path):
    class Early(Toy):
        frame = rows(published=["2026-03-01", "2026-07-31", "2026-08-10"])

    class Empty(Toy):
        frame = rows().iloc[0:0]
    for src in (Early(), Empty()):
        with pytest.raises(ST.Refused):
            ST.pull(src, REQ, tmp_path, TODAY)
    assert not (tmp_path / "toy_quarterly").exists()


def test_the_newest_pull_is_read_unless_another_is_named_and_a_changed_file_is_refused(tmp_path):
    ST.pull(Toy(), REQ, tmp_path, dt.date(2026, 11, 1))

    class Revised(Toy):
        frame = rows(sales=[1.5, 2.0, 5.0])
    ST.pull(Revised(), REQ, tmp_path, TODAY)
    assert ST.load(Toy(), root=tmp_path)["sales"].iloc[0] == 1.5
    assert ST.load(Toy(), pulled="2026-11-01", root=tmp_path)["sales"].iloc[0] == 1.0
    f = tmp_path / "toy_quarterly" / "2026-11-17" / "data.parquet"
    pd.read_parquet(f).assign(sales=9.0).to_parquet(f, index=False)
    with pytest.raises(ValueError, match="sha256"):
        ST.load(Toy(), root=tmp_path)
    with pytest.raises(FileNotFoundError):
        ST.load(Toy(), pulled="2020-01-01", root=tmp_path)


def test_a_file_source_reads_its_export_renames_headers_and_keeps_the_requested_window(tmp_path):
    rows().rename(columns={"sales": "Net Sales"}).to_csv(tmp_path / "export.csv", index=False)

    class Export(FileSource):
        dataset = DS
        path = tmp_path / "export.csv"
        rename: ClassVar[dict[str, str]] = {"Net Sales": "sales"}
    got = Export().fetch(PullRequest(dt.date(2026, 6, 1), dt.date(2026, 12, 31), ("AAA",)))
    assert got["sales"].tolist() == [2.0]

    class Missing(Export):
        path = tmp_path / "none.csv"
    with pytest.raises(FileNotFoundError, match="export file not found"):
        Missing().fetch(REQ)


def test_every_registered_source_is_sound_and_names_an_existing_licence_row():
    names = {s.name for s in L.SOURCES}
    for name, cls in RG.sources().items():
        assert cls.dataset.problems() == [], name
        assert cls.dataset.licence in names, f"{name}: licence row {cls.dataset.licence!r} missing from quant.core.licences"


def test_a_second_pull_on_the_same_day_is_refused_and_leaves_the_stored_pull_unchanged(tmp_path):
    first = ST.pull(Toy(), REQ, tmp_path, TODAY)

    class Revised(Toy):
        frame = rows(sales=[7.0, 8.0, 9.0])
    with pytest.raises(ST.Refused, match="already pulled on"):
        ST.pull(Revised(), REQ, tmp_path, TODAY)
    assert ST.load(Toy(), root=tmp_path)["sales"].iloc[0] == 1.0 and first["sha256"] == json.loads((tmp_path / "toy_quarterly" / TODAY.isoformat() / "MANIFEST.json").read_text())["sha256"]
