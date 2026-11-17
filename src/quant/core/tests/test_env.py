"""quant.core.env: the one definition of how a credential is read from the environment."""
import pytest

from quant.core.env import env


def test_a_set_value_is_returned_without_surrounding_space_and_an_absent_one_is_empty(monkeypatch):
    monkeypatch.setenv("QUANT_TEST_KEY", "  abc \n")
    monkeypatch.delenv("QUANT_TEST_ABSENT", raising=False)
    assert env("QUANT_TEST_KEY") == "abc" and env("QUANT_TEST_ABSENT") == ""


def test_a_value_still_in_its_encrypted_form_is_refused_and_the_name_is_reported_but_not_the_value(monkeypatch):
    monkeypatch.setenv("QUANT_TEST_KEY", "encrypted:SECRETPAYLOAD")
    with pytest.raises(RuntimeError, match="QUANT_TEST_KEY is still encrypted") as e:
        env("QUANT_TEST_KEY")
    assert "SECRETPAYLOAD" not in str(e.value)


def test_both_broker_modules_use_this_definition():
    from quant.execution.brokers.alpaca import api as alpaca

    assert alpaca.env is env
