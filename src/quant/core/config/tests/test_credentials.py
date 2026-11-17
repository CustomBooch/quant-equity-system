"""config.credentials: values come only from the environment (dotenvx decrypts the .env into it); ciphertext is refused, absence is reported by name."""
from __future__ import annotations

import pytest

from quant.core.config import credentials as C


class TestGet:
    def test_a_set_value_is_returned_trimmed_and_an_empty_one_is_none(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "  abc  ")
        monkeypatch.setenv("EIA_API_KEY", "")
        assert C._get("FRED_API_KEY") == "abc" and C._get("EIA_API_KEY") is None

    def test_a_value_still_encrypted_by_dotenvx_refuses(self, monkeypatch):
        monkeypatch.setenv("WRDS_PASSWORD", "encrypted:BOx8ab")
        with pytest.raises(OSError, match="WRDS_PASSWORD is still encrypted"):
            C._get("WRDS_PASSWORD")


class TestCheckCredentials:
    def test_a_required_credential_that_is_missing_refuses_by_name(self, monkeypatch):
        monkeypatch.setattr(C, "WRDS_USERNAME", None)
        with pytest.raises(OSError, match=r"\['WRDS_USERNAME'\].*dotenvx run"):
            C.check_credentials(["WRDS_USERNAME"])
