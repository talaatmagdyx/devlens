"""Single-operator UI authentication.

What this provides, stated plainly: one shared password, bounded sessions that
expire, a rotating opaque token, and a per-client attempt quota. What it does
not provide: identity, roles, audit of who did what, or SSO. It is adequate for
one operator on a loopback interface and is not an authentication boundary for
a shared network — the hosted entry point exists for that.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from dataclasses import dataclass

from devlens.domain import AccessDenied

COOKIE = "devlens_session"
DEFAULT_TTL = 8 * 3600
MAX_SESSIONS = 64
MIN_PASSWORD_LENGTH = 12


@dataclass
class Attempt:
    failures: int = 0
    blocked_until: float = 0.0


class LoginQuota:
    """Exponential back-off after repeated failures, keyed by client."""

    def __init__(self, threshold: int = 5, base_seconds: float = 2.0, cap: float = 300.0):
        self.threshold = threshold
        self.base_seconds = base_seconds
        self.cap = cap
        self._attempts: dict[str, Attempt] = {}

    def check(self, client: str) -> None:
        attempt = self._attempts.get(client)
        if attempt and attempt.blocked_until > time.monotonic():
            wait = int(attempt.blocked_until - time.monotonic()) + 1
            raise AccessDenied(f"Too many attempts. Retry in {wait}s.")

    def record_failure(self, client: str) -> None:
        attempt = self._attempts.setdefault(client, Attempt())
        attempt.failures += 1
        if attempt.failures >= self.threshold:
            delay = min(
                self.cap, self.base_seconds * 2 ** (attempt.failures - self.threshold)
            )
            attempt.blocked_until = time.monotonic() + delay
        if len(self._attempts) > 4096:  # pragma: no cover - pathological client churn
            self._attempts.clear()

    def record_success(self, client: str) -> None:
        self._attempts.pop(client, None)


class AuthGate:
    """Password gate with expiring, rotating sessions."""

    def __init__(
        self,
        password: str | None = None,
        ttl: int = DEFAULT_TTL,
        max_sessions: int = MAX_SESSIONS,
    ):
        self.password = password or None
        self.ttl = ttl
        self.max_sessions = max_sessions
        self.sessions: dict[str, float] = {}
        self.quota = LoginQuota()

    @classmethod
    def from_env(cls) -> AuthGate:
        raw = os.environ.get("DEVLENS_UI_PASSWORD", "").strip()
        ttl = int(os.environ.get("DEVLENS_SESSION_SECONDS", DEFAULT_TTL) or DEFAULT_TTL)
        return cls(raw or None, ttl=max(60, min(ttl, 86_400)))

    @property
    def required(self) -> bool:
        return self.password is not None

    @property
    def weak_password(self) -> bool:
        """True when a password is set but short enough to be worth warning about."""
        return bool(self.password) and len(self.password or "") < MIN_PASSWORD_LENGTH

    def login(self, password: str, client: str = "local") -> str:
        if not self.required:
            return ""
        self.quota.check(client)
        expected = (self.password or "").encode("utf-8")
        supplied = (password or "").encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            self.quota.record_failure(client)
            raise AccessDenied("Invalid password.")
        self.quota.record_success(client)
        return self._issue()

    def _issue(self) -> str:
        self._sweep()
        if len(self.sessions) >= self.max_sessions:
            oldest = min(self.sessions, key=self.sessions.__getitem__)
            self.sessions.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.monotonic() + self.ttl
        return token

    def _sweep(self) -> None:
        now = time.monotonic()
        for token in [t for t, expiry in self.sessions.items() if expiry <= now]:
            self.sessions.pop(token, None)

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def check(self, token: str | None) -> bool:
        if not self.required:
            return True
        if not token:
            return False
        expiry = self.sessions.get(token)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            self.sessions.pop(token, None)
            return False
        return True

    def refresh(self, token: str | None) -> str | None:
        """Rotate a valid session token, limiting the value of a stolen cookie."""
        if not self.required or not self.check(token):
            return None
        self.sessions.pop(token, None)  # type: ignore[arg-type]
        return self._issue()
