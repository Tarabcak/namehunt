"""Shared test configuration.

Spec §1.8: the pytest suite must run with ZERO network access. This autouse
fixture makes any accidental real connection fail loudly instead of silently
reaching the internet.
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("real network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
