"""Rate limiting, circuit breaking and timeouts, scoped per upstream host.

Two corrections over a naive implementation:

* A policy belongs to **one upstream**. Sharing a breaker between Jira and
  GitHub means a Jira outage stops repository reads, which is the opposite of
  a bulkhead.
* A timeout **counts as a failure**. ``asyncio.timeout`` cancels the inner
  awaitable, so a breaker that only catches :class:`Exception` never sees it
  and never opens under the most common real failure mode.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from devlens.domain import ProviderError


class CircuitOpenError(ProviderError):
    def __init__(self, provider: str = "Provider"):
        super().__init__(f"{provider} circuit is open; calls are being shed.")


class RateLimiter:
    """Token-bucket limiter that paces outbound calls to one upstream."""

    def __init__(self, rate: float = 20.0, burst: float = 40.0):
        if rate <= 0 or burst < 1:
            raise ValueError("Rate limiter requires a positive rate and burst >= 1")
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.burst, self.tokens + (now - self.updated) * self.rate
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            await asyncio.sleep(wait)


class CircuitBreaker:
    """Three-state breaker: closed, open, half-open."""

    def __init__(self, failure_threshold: int = 5, recovery_seconds: float = 30.0):
        if failure_threshold < 1 or recovery_seconds <= 0:
            raise ValueError("Invalid circuit breaker configuration")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.failures = 0
        self.opened_at = 0.0
        self.state = "closed"
        self._lock = asyncio.Lock()

    async def call(self, factory):
        async with self._lock:
            if self.state == "open":
                if time.monotonic() - self.opened_at >= self.recovery_seconds:
                    self.state = "half_open"
                else:
                    raise CircuitOpenError()
        try:
            result = await factory()
        except asyncio.CancelledError:
            # An external cancellation is not the provider's fault and must not
            # be counted against it. A timeout is recorded by ResiliencePolicy.
            raise
        except Exception:
            await self.record_failure()
            raise
        await self.record_success()
        return result

    async def record_success(self) -> None:
        async with self._lock:
            self.failures = 0
            self.state = "closed"

    async def record_failure(self) -> None:
        async with self._lock:
            self.failures += 1
            if self.failures >= self.failure_threshold or self.state == "half_open":
                self.state = "open"
                self.opened_at = time.monotonic()


class ResiliencePolicy:
    """Rate limit, circuit breaker and timeout for a single upstream."""

    def __init__(
        self,
        limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        timeout: float = 15.0,
        name: str = "provider",
    ):
        self.limiter = limiter or RateLimiter()
        self.breaker = breaker or CircuitBreaker()
        self.timeout = timeout
        self.name = name

    async def call(self, factory):
        await self.limiter.acquire()
        try:
            async with asyncio.timeout(self.timeout):
                return await self.breaker.call(factory)
        except CircuitOpenError:
            raise CircuitOpenError(self.name) from None
        except TimeoutError:
            # asyncio.timeout raises TimeoutError here after cancelling the
            # inner awaitable, so the breaker never saw the failure itself.
            await self.breaker.record_failure()
            raise ProviderError(
                f"{self.name} call exceeded its {self.timeout:g}s time budget."
            ) from None


class PolicyRegistry:
    """One policy per upstream host, created on demand.

    Clients for different hosts never share a breaker, so an outage is
    contained to the provider that is actually failing.
    """

    def __init__(self, rate: float, burst: float, threshold: int, recovery: float, timeout: float):
        self._settings = (rate, burst, threshold, recovery, timeout)
        self._policies: dict[str, ResiliencePolicy] = {}

    def for_host(self, url: str) -> ResiliencePolicy:
        host = urlsplit(str(url)).hostname or str(url)
        if host not in self._policies:
            rate, burst, threshold, recovery, timeout = self._settings
            self._policies[host] = ResiliencePolicy(
                RateLimiter(rate, burst),
                CircuitBreaker(threshold, recovery),
                timeout=timeout,
                name=host,
            )
        return self._policies[host]

    def attach_to(self, client) -> ResiliencePolicy:
        policy = self.for_host(str(getattr(client, "base_url", "")))
        attach(client, policy)
        return policy


def attach(client, policy: ResiliencePolicy) -> None:
    client.resilience = policy
