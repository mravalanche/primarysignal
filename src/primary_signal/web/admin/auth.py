"""Single-operator authentication primitives for the private admin origin.

No bearer value is written to the database. Only HMAC digests of opaque
session identifiers and source addresses cross the storage boundary.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

COOKIE_NAME = "__Host-primary_signal_admin"
COOKIE_MAX_AGE = 12 * 60 * 60
_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)


@dataclass(frozen=True)
class AdminAuthConfig:
    """Explicit deployment configuration; values must come from private config."""

    origin: str
    password_phc: str
    session_key: bytes
    idle_seconds: int = 30 * 60
    absolute_seconds: int = COOKIE_MAX_AGE
    source_attempts: int = 5
    global_attempts: int = 20
    attempt_window_seconds: int = 15 * 60

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("admin origin must be an HTTPS origin without a path")
        if parsed.username or parsed.password:
            raise ValueError("admin origin must not contain credentials")
        if not self.password_phc.startswith("$argon2id$"):
            raise ValueError("admin password must be an Argon2id PHC hash")
        if len(self.session_key) < 32:
            raise ValueError("admin session key must contain at least 32 bytes")
        if not (0 < self.idle_seconds <= self.absolute_seconds <= 86400):
            raise ValueError("admin session lifetimes are invalid")
        if min(self.source_attempts, self.global_attempts, self.attempt_window_seconds) < 1:
            raise ValueError("admin login rate limits are invalid")

    @property
    def host(self) -> str:
        return urlsplit(self.origin).netloc


@dataclass(frozen=True)
class StoredSession:
    digest: bytes
    csrf_secret: bytes
    created_at: datetime
    last_seen_at: datetime


class SessionStore(Protocol):
    """Atomic storage operations needed by the authentication service."""

    def claim_login_attempt(
        self,
        source_digest: bytes,
        *,
        since: datetime,
        at: datetime,
        source_limit: int,
        global_limit: int,
    ) -> bool: ...
    def create(self, session: StoredSession) -> None: ...
    def find_and_touch(
        self, digest: bytes, *, now: datetime, idle_since: datetime, absolute_since: datetime
    ) -> StoredSession | None: ...
    def revoke(self, digest: bytes) -> None: ...


@dataclass(frozen=True)
class LoginResult:
    identifier: str
    csrf_token: str


class LoginRateLimited(Exception):
    """The bounded login-attempt window has been exceeded."""


class AdminAuthService:
    def __init__(self, config: AdminAuthConfig, store: SessionStore) -> None:
        self.config = config
        self.store = store
        # Either credential rotation invalidates all previously issued IDs.
        self._digest_key = hmac.digest(
            config.session_key, config.password_phc.encode("utf-8"), hashlib.sha256
        )

    def _digest(self, kind: bytes, value: str) -> bytes:
        return hmac.digest(self._digest_key, kind + value.encode("utf-8"), hashlib.sha256)

    def _identifier_digest(self, identifier: str) -> bytes | None:
        try:
            raw = bytes.fromhex(identifier)
        except ValueError:
            return None
        if len(raw) != 32:
            return None
        return self._digest(b"session:", identifier)

    def login(
        self, password: str, source_address: str, *, now: datetime | None = None
    ) -> LoginResult | None:
        now = now or datetime.now(UTC)
        source = self._digest(b"source:", source_address)
        if not self.store.claim_login_attempt(
            source,
            since=now - timedelta(seconds=self.config.attempt_window_seconds),
            at=now,
            source_limit=self.config.source_attempts,
            global_limit=self.config.global_attempts,
        ):
            raise LoginRateLimited
        try:
            verified = _HASHER.verify(self.config.password_phc, password)
        except InvalidHashError, VerificationError, VerifyMismatchError:
            verified = False
        if not verified:
            return None
        identifier = secrets.token_hex(32)
        csrf = secrets.token_hex(32)
        self.store.create(
            StoredSession(
                digest=self._digest(b"session:", identifier),
                csrf_secret=bytes.fromhex(csrf),
                created_at=now,
                last_seen_at=now,
            )
        )
        return LoginResult(identifier=identifier, csrf_token=csrf)

    def authenticate(
        self, identifier: str | None, *, now: datetime | None = None
    ) -> StoredSession | None:
        if identifier is None or (digest := self._identifier_digest(identifier)) is None:
            return None
        now = now or datetime.now(UTC)
        return self.store.find_and_touch(
            digest,
            now=now,
            idle_since=now - timedelta(seconds=self.config.idle_seconds),
            absolute_since=now - timedelta(seconds=self.config.absolute_seconds),
        )

    @staticmethod
    def valid_csrf(session: StoredSession, token: str | None) -> bool:
        if token is None:
            return False
        try:
            received = bytes.fromhex(token)
        except ValueError:
            return False
        return hmac.compare_digest(session.csrf_secret, received)

    def logout(self, identifier: str) -> None:
        if (digest := self._identifier_digest(identifier)) is not None:
            self.store.revoke(digest)
