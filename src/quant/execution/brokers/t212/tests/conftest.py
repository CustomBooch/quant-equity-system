"""Fixtures shared by the lab.t212 tests. No test in this package reaches the network: the broker's HTTP is replaced at its boundary (urlopen, or the opener the
read client builds), and a socket connection attempted anyway fails the test instead of leaving the machine."""
from __future__ import annotations

import socket
from typing import NoReturn

import pytest

from quant.execution import ledger as LG
from quant.execution.brokers.t212.tests import factories as F
from quant.execution.brokers.t212.tests.factories import Broker, Clock


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("a lab.t212 test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture()
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invented read and full key pairs in the environment (the read client must only ever use the read pair)."""
    for k, v in {"T212_READ_API_KEY": "k-read", "T212_READ_SECRET_KEY": "s-read", "T212_FULL_API_KEY": "k-full", "T212_FULL_SECRET_KEY": "s-full"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def broker(monkeypatch: pytest.MonkeyPatch) -> Broker:
    return Broker().install(monkeypatch)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def engine(tmp_path, monkeypatch, keys):
    """The engine stand-in with tranche 0 holding 4 BBB and tranche 1 empty, and the broker reporting the same position."""
    e = F.Engine(tmp_path, monkeypatch)
    LG.record_batch({}, 0, {"BBB_US_EQ": 4.0})
    e.positions = [F.position("BBB_US_EQ", 4.0, 19.0)]
    return e
