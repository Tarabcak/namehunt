"""RDAP domain-availability checks with an optional DNS-over-HTTPS pre-filter.

CORRECTNESS-CRITICAL (spec §1.5): every failure path maps to UNKNOWN, never
AVAILABLE. The only AVAILABLE path is an authoritative RDAP 404.
"""

from __future__ import annotations

import enum
import os
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx
import whoisit
from whoisit.errors import RateLimitedError, ResourceDoesNotExist

from namehunt.session import state_home

DOH_URL = "https://cloudflare-dns.com/dns-query"
RETRY_BACKOFF_SECONDS = (2.0, 8.0)  # 429 → wait 2s, retry; 429 again → wait 8s, retry
DEFAULT_MAX_PER_SEC = 4.0
DEFAULT_MAX_IN_FLIGHT = 4
BOOTSTRAP_MAX_AGE_DAYS = 7

PrefilterVerdict = Literal["resolves", "nxdomain", "inconclusive"]
SleepFn = Callable[[float], None]


class Availability(enum.Enum):
    AVAILABLE = "available"
    TAKEN = "taken"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DomainCheck:
    domain: str
    status: Availability
    detail: str = ""  # failure reason when UNKNOWN, surfaced with --debug


class Throttle(Protocol):
    def acquire(self) -> None: ...


class RateLimiter:
    """Global request throttle: at most max_per_sec acquisitions per second."""

    def __init__(
        self,
        max_per_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: SleepFn = time.sleep,
    ) -> None:
        if max_per_sec <= 0:
            raise ValueError("max_per_sec must be positive")
        self._interval = 1.0 / max_per_sec
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = clock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next_allowed:
                self._sleep(self._next_allowed - now)
                now = self._clock()
            self._next_allowed = max(now, self._next_allowed) + self._interval


def ensure_bootstrap(
    cache_path: Path | None = None, *, max_age_days: int = BOOTSTRAP_MAX_AGE_DAYS
) -> bool:
    """Load or refresh whoisit's IANA bootstrap data; False → callers must report UNKNOWN."""
    cache = cache_path if cache_path is not None else state_home() / "bootstrap.json"
    try:
        if whoisit.is_bootstrapped():
            return True
    except Exception:
        pass
    if _load_fresh_cache(cache, max_age_days):
        return True
    try:
        whoisit.bootstrap()
        data = whoisit.save_bootstrap_data()
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_text(data if isinstance(data, str) else str(data))
        os.replace(tmp, cache)
        return True
    except Exception:
        return False


def _load_fresh_cache(cache: Path, max_age_days: int) -> bool:
    try:
        age_seconds = time.time() - cache.stat().st_mtime
        if age_seconds > max_age_days * 86400:
            return False
        whoisit.load_bootstrap_data(cache.read_text())
        return True
    except Exception:
        return False


def check_domain(
    domain: str,
    *,
    sleep: SleepFn = time.sleep,
    limiter: Throttle | None = None,
) -> DomainCheck:
    """Single RDAP lookup. Never raises; every failure maps to UNKNOWN."""
    for attempt in range(1 + len(RETRY_BACKOFF_SECONDS)):
        if limiter is not None:
            limiter.acquire()
        try:
            result = whoisit.domain(domain, follow_related=False)
        except ResourceDoesNotExist:
            return DomainCheck(domain, Availability.AVAILABLE)
        except RateLimitedError:
            if attempt < len(RETRY_BACKOFF_SECONDS):
                sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            return DomainCheck(domain, Availability.UNKNOWN, "rate limited after retries")
        except Exception as exc:  # timeout, connection, RDAP/HTTP, parse, unsupported TLD
            return DomainCheck(domain, Availability.UNKNOWN, f"{type(exc).__name__}: {exc}")
        if isinstance(result, dict) and result:
            return DomainCheck(domain, Availability.TAKEN)
        return DomainCheck(domain, Availability.UNKNOWN, "empty rdap response")
    return DomainCheck(domain, Availability.UNKNOWN, "retries exhausted")  # pragma: no cover


def doh_prefilter(domain: str, client: httpx.Client, *, timeout: float = 5.0) -> PrefilterVerdict:
    """DNS heuristic: "resolves" may short-circuit to TAKEN; "nxdomain" still
    needs RDAP confirmation; any ambiguity or failure is "inconclusive"."""
    try:
        resp = client.get(
            DOH_URL,
            params={"name": domain, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return "inconclusive"
        data = resp.json()
        status = data.get("Status")
        if status == 3:
            return "nxdomain"
        if status == 0 and data.get("Answer"):
            return "resolves"
        return "inconclusive"
    except Exception:
        return "inconclusive"


def check_domains(
    stems: Sequence[str],
    tlds: Sequence[str],
    *,
    prefilter: Callable[[str], str] | None = None,
    limiter: Throttle | None = None,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
) -> dict[str, DomainCheck]:
    """Check every stem x TLD, throttled and concurrency-bounded.
    Bootstrap failure → every domain UNKNOWN, RDAP never attempted."""
    domains = [f"{stem}.{tld}" for stem in stems for tld in tlds]
    if not domains:
        return {}
    if not ensure_bootstrap():
        return {d: DomainCheck(d, Availability.UNKNOWN, "rdap bootstrap failed") for d in domains}
    throttle: Throttle = limiter if limiter is not None else RateLimiter(DEFAULT_MAX_PER_SEC)

    def check_one(domain: str) -> DomainCheck:
        if prefilter is not None:
            try:
                verdict = prefilter(domain)
            except Exception:
                verdict = "inconclusive"
            if verdict == "resolves":
                return DomainCheck(domain, Availability.TAKEN, "dns resolves")
        return check_domain(domain, limiter=throttle)

    with ThreadPoolExecutor(max_workers=min(max_in_flight, len(domains))) as pool:
        checks = list(pool.map(check_one, domains))
    return {check.domain: check for check in checks}
