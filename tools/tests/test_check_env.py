"""tools/check_env.py: lock files are parsed and package names normalised as pip does."""
from __future__ import annotations

from importlib import metadata

from tools_loader import load

env = load("check_env")


def test_compare_separates_matching_missing_differing_and_known_unobtainable_pins():
    pins = {"pandas": "2.0", "numpy": "2.1", "scipy": "1.0", "torch": "2.12.0.dev"}
    ok, missing, differ, unobtainable = env.compare(pins, {"pandas": "2.0", "numpy": "2.2", "torch": "2.11"})
    assert ok == ["pandas"] and missing == [("scipy", "1.0")] and differ == [("numpy", "2.1", "2.2")] and unobtainable == [("torch", "2.12.0.dev", "2.11")]


def test_strict_mode_fails_only_on_a_missing_or_differing_pin_in_this_environment(tmp_path):
    pandas = metadata.version("pandas")
    good, bad = tmp_path / "good.txt", tmp_path / "bad.txt"
    good.write_text(f"pandas=={pandas}\ntorch==0.0.1\n", encoding="utf-8")         # torch is known-unobtainable: reported, never failing
    bad.write_text(f"pandas=={pandas}\nno-such-package-anywhere==1.0\n", encoding="utf-8")
    assert env.main(["--lock", str(good), "--strict"]) == 0
    assert env.main(["--lock", str(bad), "--strict"]) == 1 and env.main(["--lock", str(bad)]) == 0      # without --strict it only reports


def test_env_checker_parses_pins_and_normalises_names(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text("# comment\nSciPy==1.2.3\nzope.interface==5.0\n\nnot a pin\n", encoding="utf-8")
    assert env.read_lock(lock) == {"scipy": "1.2.3", "zope-interface": "5.0"}
    assert env.norm("Typing_Extensions") == "typing-extensions"
