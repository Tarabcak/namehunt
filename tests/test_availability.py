"""Tests for namehunt.availability — spec §1.8 items 1, 2 and 7.

CORRECTNESS-CRITICAL contract (spec §1.5): availability is a three-state enum
and every failure path (timeout, connection error, RDAP/HTTP error, 429 after
retries, bootstrap failure, parse failure, unsupported TLD) maps to UNKNOWN —
never AVAILABLE. All whoisit and DoH network calls are mocked.
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpx
import pytest
import requests
from namehunt.availability import (
    Availability,
    RateLimiter,
    check_domain,
    check_domains,
    doh_prefilter,
)
from whoisit.errors import (
    BootstrapError,
    ParseError,
    QueryError,
    RateLimitedError,
    ResourceDoesNotExist,
    UnsupportedError,
)

from namehunt import availability


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class SpyLimiter:
    """Thread-safe acquire counter standing in for RateLimiter."""

    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            self.count += 1


def raiser(exc: BaseException):
    def fake(domain, **kwargs):
        raise exc

    return fake


def patch_domain(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(availability.whoisit, "domain", fn)


def allow_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(availability, "ensure_bootstrap", lambda *a, **k: True)


# ---------------------------------------------------------------- RDAP mapping


def test_rdap_404_is_available(monkeypatch):
    patch_domain(monkeypatch, raiser(ResourceDoesNotExist("no such domain")))
    result = check_domain("zzqx.com")
    assert result.status is Availability.AVAILABLE
    assert result.domain == "zzqx.com"


def test_rdap_registered_is_taken(monkeypatch):
    patch_domain(monkeypatch, lambda domain, **kw: {"handle": "ZZQX", "status": ["active"]})
    assert check_domain("zzqx.com").status is Availability.TAKEN


@pytest.mark.parametrize("weird", [None, {}])
def test_rdap_empty_response_is_unknown(monkeypatch, weird):
    """A 200 that parses to nothing is neither AVAILABLE nor TAKEN."""
    patch_domain(monkeypatch, lambda domain, **kw: weird)
    assert check_domain("zzqx.com").status is Availability.UNKNOWN


def test_rdap_429_after_retries_is_unknown(monkeypatch):
    calls: list[str] = []

    def fake(domain, **kwargs):
        calls.append(domain)
        raise RateLimitedError("429 slow down")

    patch_domain(monkeypatch, fake)
    sleeps: list[float] = []
    result = check_domain("zzqx.com", sleep=sleeps.append)
    assert result.status is Availability.UNKNOWN
    assert sleeps == [2, 8]
    assert len(calls) == 3


def test_rdap_429_then_success_recovers(monkeypatch):
    calls: list[str] = []

    def fake(domain, **kwargs):
        calls.append(domain)
        if len(calls) == 1:
            raise RateLimitedError("429")
        raise ResourceDoesNotExist("404")

    patch_domain(monkeypatch, fake)
    sleeps: list[float] = []
    result = check_domain("zzqx.com", sleep=sleeps.append)
    assert result.status is Availability.AVAILABLE
    assert sleeps == [2]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.ConnectionError("connection refused"),
        QueryError("rdap server returned 500"),
        ParseError("unparseable rdap payload"),
        UnsupportedError("no rdap endpoint for this tld"),
        RuntimeError("anything unexpected"),
    ],
)
def test_every_failure_is_unknown_never_available(monkeypatch, exc):
    patch_domain(monkeypatch, raiser(exc))
    result = check_domain("zzqx.com")
    assert result.status is Availability.UNKNOWN
    assert result.detail  # failure reason is recorded for --debug


# ------------------------------------------------------------------ bootstrap


def test_ensure_bootstrap_fresh_cache_skips_network(tmp_path, monkeypatch):
    cache = tmp_path / "bootstrap.json"
    cache.write_text('{"cached": true}')
    fetched: list[bool] = []
    loaded: list[object] = []
    monkeypatch.setattr(availability.whoisit, "bootstrap", lambda **kw: fetched.append(True))
    monkeypatch.setattr(
        availability.whoisit,
        "load_bootstrap_data",
        lambda data, **kw: loaded.append(data) or True,
    )
    assert availability.ensure_bootstrap(cache) is True
    assert fetched == []
    assert loaded  # the cached payload was handed to whoisit


def test_ensure_bootstrap_stale_cache_refetches(tmp_path, monkeypatch):
    cache = tmp_path / "bootstrap.json"
    cache.write_text('{"cached": true}')
    stale = time.time() - 8 * 86400
    os.utime(cache, (stale, stale))
    monkeypatch.setattr(availability.whoisit, "bootstrap", lambda **kw: None)
    monkeypatch.setattr(availability.whoisit, "save_bootstrap_data", lambda **kw: '{"fresh": 1}')
    assert availability.ensure_bootstrap(cache, max_age_days=7) is True
    assert json.loads(cache.read_text()) == {"fresh": 1}


def test_ensure_bootstrap_failure_returns_false(tmp_path, monkeypatch):
    cache = tmp_path / "bootstrap.json"  # deliberately absent

    def boom(**kwargs):
        raise BootstrapError("iana unreachable")

    monkeypatch.setattr(availability.whoisit, "bootstrap", boom)
    assert availability.ensure_bootstrap(cache) is False


def test_bootstrap_failure_makes_all_checks_unknown(monkeypatch):
    monkeypatch.setattr(availability, "ensure_bootstrap", lambda *a, **k: False)
    rdap_calls: list[str] = []
    patch_domain(monkeypatch, lambda domain, **kw: rdap_calls.append(domain))
    results = check_domains(["nova", "quill"], ["com", "ai"])
    assert set(results) == {"nova.com", "nova.ai", "quill.com", "quill.ai"}
    assert all(r.status is Availability.UNKNOWN for r in results.values())
    assert rdap_calls == []  # RDAP never attempted without bootstrap


# ------------------------------------------------------------- DoH prefilter


def doh_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_doh_nxdomain_and_request_shape():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["name"] = request.url.params.get("name")
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, json={"Status": 3})

    with doh_client(handler) as client:
        assert doh_prefilter("zzqx.com", client) == "nxdomain"
    assert seen["host"] == "cloudflare-dns.com"
    assert seen["name"] == "zzqx.com"
    assert seen["accept"] == "application/dns-json"


def test_doh_resolving_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        answer = [{"name": "x.com.", "type": 1, "data": "192.0.2.1"}]
        return httpx.Response(200, json={"Status": 0, "Answer": answer})

    with doh_client(handler) as client:
        assert doh_prefilter("x.com", client) == "resolves"


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"status_code": 200, "json": {"Status": 0}},  # NOERROR but no answers
        {"status_code": 200, "json": {"Status": 2}},  # SERVFAIL
        {"status_code": 500, "text": "boom"},
        {"status_code": 200, "content": b"not json"},
    ],
)
def test_doh_ambiguity_is_inconclusive(response_kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(**response_kwargs)

    with doh_client(handler) as client:
        assert doh_prefilter("x.com", client) == "inconclusive"


def test_doh_network_error_is_inconclusive():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with doh_client(handler) as client:
        assert doh_prefilter("x.com", client) == "inconclusive"


def test_prefilter_resolves_short_circuits_to_taken_without_rdap(monkeypatch):
    allow_bootstrap(monkeypatch)
    rdap_calls: list[str] = []

    def fake(domain, **kwargs):
        rdap_calls.append(domain)
        raise ResourceDoesNotExist("404")

    patch_domain(monkeypatch, fake)
    verdicts = {"parked.com": "resolves", "ghost.com": "nxdomain"}
    results = check_domains(["parked", "ghost"], ["com"], prefilter=lambda d: verdicts[d])
    assert results["parked.com"].status is Availability.TAKEN
    assert "parked.com" not in rdap_calls
    # NXDOMAIN alone is a heuristic: AVAILABLE only after RDAP confirmation.
    assert results["ghost.com"].status is Availability.AVAILABLE
    assert "ghost.com" in rdap_calls


def test_prefilter_nxdomain_never_yields_available_without_rdap_success(monkeypatch):
    allow_bootstrap(monkeypatch)
    patch_domain(monkeypatch, raiser(QueryError("rdap down")))
    results = check_domains(["ghost"], ["com"], prefilter=lambda d: "nxdomain")
    assert results["ghost.com"].status is Availability.UNKNOWN


def test_prefilter_inconclusive_falls_through_to_rdap(monkeypatch):
    allow_bootstrap(monkeypatch)
    patch_domain(monkeypatch, lambda domain, **kw: {"handle": "X"})
    results = check_domains(["odd"], ["com"], prefilter=lambda d: "inconclusive")
    assert results["odd.com"].status is Availability.TAKEN


# ------------------------------------------------------------------- throttle


def test_rate_limiter_never_exceeds_cap():
    """Spec §1.8 item 7: 50 queued checks never exceed 4 req/s (fake clock)."""
    clk = FakeClock()
    limiter = RateLimiter(4.0, clock=clk.now, sleep=clk.sleep)
    stamps: list[float] = []
    for _ in range(50):
        limiter.acquire()
        stamps.append(clk.now())
    for t in stamps:
        in_window = [s for s in stamps if 0 <= t - s < 1.0 - 1e-6]
        assert len(in_window) <= 4
    assert stamps[-1] >= 11.0  # 50 requests at <=4/s cannot finish faster


def test_check_domains_throttles_every_request(monkeypatch):
    allow_bootstrap(monkeypatch)
    patch_domain(monkeypatch, lambda domain, **kw: {"handle": "X"})
    spy = SpyLimiter()
    results = check_domains(["aaa", "bbb", "ccc"], ["com", "ai"], limiter=spy)
    assert len(results) == 6
    assert spy.count == 6


def test_retries_also_pass_through_limiter(monkeypatch):
    patch_domain(monkeypatch, raiser(RateLimitedError("429")))
    spy = SpyLimiter()
    result = check_domain("zzqx.com", sleep=lambda s: None, limiter=spy)
    assert result.status is Availability.UNKNOWN
    assert spy.count == 3
