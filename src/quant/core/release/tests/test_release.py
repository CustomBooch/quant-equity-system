from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quant.core.release import ReleaseError, require_validation, sha256_file, verify_release, write_release


@pytest.fixture
def rel(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n2\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hello")
    write_release(tmp_path, "t_v1", "test", inputs={"up": "00"}, expected={"n": 2}, code_commit="abc")
    return tmp_path


def test_a_written_release_verifies_and_lists_every_file_with_its_hash(rel):
    doc = verify_release(rel)
    assert doc["release"] == "t_v1" and set(doc["files"]) == {"a.csv", "sub/b.txt"} and doc["expected"] == {"n": 2}
    assert doc["files"]["sub/b.txt"]["sha256"] == sha256_file(rel / "sub" / "b.txt")


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: (d / "a.csv").write_text("x\n1\n3\n"), "hash differs"),
    (lambda d: (d / "a.csv").write_text("x\n1\n2\n\n"), "size differs"),
    (lambda d: (d / "sub" / "b.txt").unlink(), "missing"),
    (lambda d: (d / "extra.csv").write_text("sneaked in"), "not listed"),
])
def test_a_changed_missing_or_unlisted_file_fails_verification(rel, mutate, msg):
    mutate(rel)
    with pytest.raises(ReleaseError, match=msg):
        verify_release(rel)


def test_an_existing_release_is_never_overwritten_and_an_unknown_schema_is_refused(rel):
    with pytest.raises(ReleaseError, match="already exists"):
        write_release(rel, "t_v1")
    m = json.loads((rel / "VERSION.json").read_text())
    m["schema"] = "release/2"
    (rel / "VERSION.json").write_text(json.dumps(m))
    with pytest.raises(ReleaseError, match="unsupported schema"):
        verify_release(rel)


def test_an_empty_directory_cannot_become_a_release(tmp_path):
    with pytest.raises(ReleaseError, match="no files"):
        write_release(tmp_path, "empty")


def test_the_example_release_shared_with_r_and_rust_verifies():
    """The committed synthetic example is the same one the R and Rust verifiers are tested on."""
    doc = verify_release(Path(__file__).resolve().parents[5] / "contracts" / "examples" / "release_example_v1")       # src/quant/core/release/tests -> the repository
    assert doc["release"] == "release_example_v1" and doc["expected"] == {"n": 3}


def _validated(tmp_path, gates, report_hash=None):
    (tmp_path / "table.txt").write_text("t")
    (tmp_path / "validation_report.txt").write_text("all good")
    h = report_hash or sha256_file(tmp_path / "validation_report.txt")
    write_release(tmp_path, "layer_v1", validation={"report_sha256": h, "gates": gates}, code_commit="c")
    return verify_release(tmp_path)


def test_validation_accepts_a_release_whose_gates_all_pass(tmp_path):
    assert require_validation(_validated(tmp_path, {"G1": "pass", "G2": "pass"}))["release"] == "layer_v1"


@pytest.mark.parametrize("gates,rh,msg", [({"G1": "pass", "G2": "FAIL"}, None, "gates not passed"), ({"G1": "pending"}, None, "gates not passed"),
                                          ({}, None, "no gates"), ({"G1": "pass"}, "0" * 64, "not that of a listed file")])
def test_validation_refuses_failed_pending_or_missing_gates_and_an_unlisted_report(tmp_path, gates, rh, msg):
    with pytest.raises(ReleaseError, match=msg):
        require_validation(_validated(tmp_path, gates, rh))


def test_validation_refuses_a_release_without_a_validation_block(rel):
    with pytest.raises(ReleaseError, match="no validation block"):
        require_validation(verify_release(rel))


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A git repository standing in for this one (R.REPO points at it), with one committed code file; `git(...)` runs git in it."""
    from quant.core.release import release as R

    root = tmp_path / "repo"
    (root / "lab").mkdir(parents=True)
    (root / "lab/x.py").write_text("a = 1\n")

    def git(*args):
        return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "core.autocrlf=false", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "first")
    monkeypatch.setattr(R, "REPO", root)
    return root, git


def release_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "a.txt").write_text("x")
    return d


def test_a_release_from_committed_code_records_the_commit_of_this_repository_wherever_it_is_run(tmp_path, repo, monkeypatch):
    from quant.core.release import release as R

    root, git = repo
    monkeypatch.chdir(tmp_path)                                                  # not the repository: git must still answer for it
    doc = R.write_release(release_dir(tmp_path, "r1"), "r1")
    assert doc["code_dirty"] is False and doc["code_commit"] == git("rev-parse", "HEAD").strip()


def test_uncommitted_code_is_refused_unless_its_state_is_recorded(tmp_path, repo):
    from quant.core.release import release as R

    root, git = repo
    (root / "lab/x.py").write_text("a = 2\n")
    with pytest.raises(R.ReleaseError, match="uncommitted"):
        R.write_release(release_dir(tmp_path, "r1"), "r1")
    doc = R.write_release(release_dir(tmp_path, "r2"), "r2", allow_dirty_code=True)
    assert doc["code_dirty"] is True and doc["code_uncommitted_files"] == ["lab/x.py"] and len(doc["code_state_sha256"]) == 64


def test_the_recorded_dirty_state_changes_with_the_uncommitted_change(tmp_path, repo):
    from quant.core.release import release as R

    root, _ = repo
    states = []
    for text in ("a = 2\n", "a = 3\n"):
        (root / "lab/x.py").write_text(text)
        states.append(R.code_state()["sha256"])
    assert states[0] != states[1]


def test_untracked_code_counts_as_uncommitted_and_other_files_do_not(tmp_path, repo):
    from quant.core.release import release as R

    root, _ = repo
    (root / "lab/notes.md").write_text("not code")
    assert R.code_state()["dirty"] is False
    (root / "lab/new.py").write_text("b = 1\n")
    assert R.code_state()["files"] == ["lab/new.py"]


def test_uncommitted_code_of_the_quant_package_under_src_counts_as_uncommitted(tmp_path, repo):
    from quant.core.release import release as R

    root, _ = repo
    (root / "src/quant").mkdir(parents=True)
    (root / "src/quant/m.py").write_text("c = 1\n")
    assert R.code_state()["files"] == ["src/quant/m.py"]


def test_in_a_repository_without_commits_untracked_code_is_still_hashed(tmp_path, monkeypatch):
    from quant.core.release import release as R

    root = tmp_path / "empty"
    (root / "lab").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "lab/x.py").write_text("a = 1\n")
    monkeypatch.setattr(R, "REPO", root)
    state = R.code_state()                                                      # git diff HEAD fails (no HEAD); the untracked file's content is hashed instead
    assert state["dirty"] is True and state["files"] == ["lab/x.py"] and len(state["sha256"]) == 64


def test_outside_a_git_repository_no_commit_and_no_code_state_are_claimed(tmp_path, monkeypatch):
    from quant.core.release import release as R

    monkeypatch.setattr(R, "REPO", tmp_path)
    doc = R.write_release(release_dir(tmp_path, "r1"), "r1")
    assert doc["code_commit"] is None and "code_dirty" not in doc


def test_an_explicit_commit_is_the_callers_statement_and_skips_the_check(tmp_path, repo):
    from quant.core.release import release as R

    root, _ = repo
    (root / "lab/x.py").write_text("a = 2\n")
    doc = R.write_release(release_dir(tmp_path, "r1"), "r1", code_commit="abc")
    assert doc["code_commit"] == "abc" and "code_dirty" not in doc


def test_the_creation_time_is_the_injected_clock_converted_to_utc(tmp_path):
    import datetime as dt

    from quant.core.release import release as R

    paris = dt.timezone(dt.timedelta(hours=2))
    doc = write_release(release_dir(tmp_path, "r1"), "r1", code_commit="abc", now=dt.datetime(2026, 10, 27, 20, 30, 5, 999, tzinfo=paris))
    assert doc["created_utc"] == "2026-10-27T18:30:05+00:00" == verify_release(tmp_path / "r1")["created_utc"]
    assert R.utc_stamp(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)) == "2026-01-02T03:04:05+00:00"


def test_a_time_without_a_zone_is_refused_before_anything_is_written(tmp_path):
    import datetime as dt

    d = release_dir(tmp_path, "r1")
    with pytest.raises(ValueError, match="no time zone"):
        write_release(d, "r1", code_commit="abc", now=dt.datetime(2026, 10, 27, 12, 0))
    assert not (d / "VERSION.json").exists()
