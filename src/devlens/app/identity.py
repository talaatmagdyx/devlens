"""Bearer and expiring browser-session authentication strategies."""
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request

from devlens.app.hosting import HostingConfig

SESSION_COOKIE = "__Host-devlens_session"


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    role: str


class IdentityStore:
    def __init__(self, config: HostingConfig):
        self.tokens: dict[bytes, Principal] = {}
        self.sessions: dict[str, tuple[Principal, float]] = {}
        self.ttl = config.session_seconds
        for subject, identity in config.identities.items():
            token = os.environ.get(identity.token_env, "")
            if len(token) < 32 or token != token.strip() or not token.isascii():
                raise ValueError("Identity tokens must be at least 32 ASCII characters; generate random tokens")
            digest = hashlib.sha256(token.encode()).digest()
            if digest in self.tokens:
                raise ValueError("Identity tokens must be unique")
            self.tokens[digest] = Principal(subject, identity.tenant, identity.role)

    def bearer(self, token: str) -> Principal | None:
        digest = hashlib.sha256(token.encode()).digest()
        for stored, principal in self.tokens.items():
            if hmac.compare_digest(digest, stored):
                return principal
        return None

    def issue(self, principal: Principal) -> str:
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}
        if len(self.sessions) >= 1000:
            raise ValueError("Session capacity reached")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (principal, now + self.ttl)
        return token

    def session(self, token: str) -> Principal | None:
        record = self.sessions.get(token)
        if record is None:
            return None
        principal, expires = record
        if expires <= time.monotonic():
            self.sessions.pop(token, None)
            return None
        return principal

    def revoke(self, token: str) -> None:
        self.sessions.pop(token, None)


class AuthenticationStrategy(Protocol):
    def authenticate(self, request: Request, store: IdentityStore) -> Principal | None: ...


class BearerAuthentication:
    def authenticate(self, request: Request, store: IdentityStore) -> Principal | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 4096:
            return None
        return store.bearer(token)


class SessionAuthentication:
    def authenticate(self, request: Request, store: IdentityStore) -> Principal | None:
        return store.session(request.cookies.get(SESSION_COOKIE, ""))


AUTHENTICATION: dict[bool, AuthenticationStrategy] = {
    True: BearerAuthentication(),
    False: SessionAuthentication(),
}


def authenticate(request: Request, store: IdentityStore) -> Principal | None:
    # An invalid Authorization header never falls back to a browser cookie.
    return AUTHENTICATION["authorization" in request.headers].authenticate(request, store)
