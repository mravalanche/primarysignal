"""Authentication boundary tests with synthetic, disposable credentials."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher, Type
from httpx import ASGITransport, AsyncClient

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.admin import create_admin_app
from primary_signal.web.admin.auth import AdminAuthConfig, AdminAuthService, StoredSession

ORIGIN = "https://admin.example.test"
PASSWORD = "synthetic-password-for-unit-tests"  # noqa: S105  # pragma: allowlist secret


class FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[bytes, StoredSession] = {}
        self.attempts: list[tuple[bytes, datetime]] = []

    def claim_login_attempt(
        self,
        source_digest: bytes,
        *,
        since: datetime,
        at: datetime,
        source_limit: int,
        global_limit: int,
    ) -> bool:
        recent = [item for item in self.attempts if item[1] >= since]
        if (
            len(recent) >= global_limit
            or sum(source == source_digest for source, _ in recent) >= source_limit
        ):
            return False
        self.attempts.append((source_digest, at))
        return True

    def create(self, session: StoredSession) -> None:
        self.sessions[session.digest] = session

    def find_and_touch(
        self, digest: bytes, *, now: datetime, idle_since: datetime, absolute_since: datetime
    ) -> StoredSession | None:
        session = self.sessions.get(digest)
        if (
            session is None
            or session.last_seen_at <= idle_since
            or session.created_at <= absolute_since
        ):
            return None
        touched = StoredSession(digest, session.csrf_secret, session.created_at, now)
        self.sessions[digest] = touched
        return touched

    def revoke(self, digest: bytes) -> None:
        self.sessions.pop(digest, None)


def auth_service(store: FakeStore | None = None) -> AdminAuthService:
    hasher = PasswordHasher(time_cost=2, memory_cost=8192, parallelism=1, type=Type.ID)
    config = AdminAuthConfig(
        origin=ORIGIN,
        password_phc=hasher.hash(PASSWORD),
        session_key=b"synthetic-test-key-with-32-bytes-minimum",  # pragma: allowlist secret
        source_attempts=3,
        global_attempts=4,
    )
    return AdminAuthService(config, store or FakeStore())


def test_production_admin_requires_authentication_config() -> None:
    with pytest.raises(RuntimeError, match="requires configured authentication"):
        create_admin_app(Settings(environment=RuntimeEnvironment.PRODUCTION))


def test_session_rotation_csrf_expiry_and_rate_limits() -> None:
    store = FakeStore()
    auth = auth_service(store)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    first = auth.login(PASSWORD, "192.0.2.10", now=now)
    assert first is not None
    assert len(first.identifier) == 64
    assert first.identifier.encode() not in repr(store.sessions).encode()
    session = auth.authenticate(first.identifier, now=now + timedelta(minutes=1))
    assert session is not None
    assert auth.valid_csrf(session, first.csrf_token)
    assert not auth.valid_csrf(session, "00" * 32)
    assert (
        auth_service(store).authenticate(first.identifier, now=now + timedelta(minutes=1)) is None
    )
    assert auth.login("wrong", "192.0.2.10", now=now) is None
    assert auth.login("wrong", "192.0.2.10", now=now) is None
    from primary_signal.web.admin.auth import LoginRateLimited

    try:
        auth.login(PASSWORD, "192.0.2.10", now=now)
    except LoginRateLimited:
        pass
    else:
        raise AssertionError("source attempt threshold was bypassed")
    assert auth.authenticate(first.identifier, now=now + timedelta(minutes=31)) is None
    digest = next(iter(store.sessions))
    store.sessions[digest] = StoredSession(
        digest, session.csrf_secret, now, now + timedelta(hours=11, minutes=59)
    )
    assert auth.authenticate(first.identifier, now=now + timedelta(hours=12, seconds=1)) is None
    assert auth.authenticate("malformed", now=now) is None
    auth.logout(first.identifier)
    assert auth.authenticate(first.identifier, now=now + timedelta(minutes=1)) is None


def test_admin_http_boundary_and_cookie() -> None:
    auth = auth_service()
    app = create_admin_app(Settings(environment=RuntimeEnvironment.TEST), auth_service=auth)

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            assert (await client.get("/admin/auth/session")).status_code == 401
            assert (
                await client.post("/admin/auth/login", json={"password": PASSWORD})
            ).status_code == 403
            malformed = await client.post(
                "/admin/auth/login",
                json={"password": PASSWORD * 300},
                headers={"Origin": ORIGIN},
            )
            assert malformed.status_code == 400
            assert PASSWORD not in malformed.text
            login = await client.post(
                "/admin/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert login.status_code == 200
            cookie = login.headers["set-cookie"]
            assert "__Host-primary_signal_admin=" in cookie
            assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
            assert "Path=/" in cookie and "Domain=" not in cookie
            assert login.headers["cache-control"] == "no-store"
            assert (await client.get("/admin/auth/session")).json() == {"authenticated": True}
            assert (
                await client.post("/admin/auth/logout", headers={"Origin": ORIGIN})
            ).status_code == 403
            assert (
                await client.post(
                    "/admin/auth/logout", headers={"x-csrf-token": login.json()["csrf_token"]}
                )
            ).status_code == 403
            logout = await client.post(
                "/admin/auth/logout",
                headers={"Origin": ORIGIN, "x-csrf-token": login.json()["csrf_token"]},
            )
            assert logout.status_code == 204
            assert (await client.get("/admin/auth/session")).status_code == 401
            assert (
                await client.get("/health/live", headers={"Host": "wrong.example.test"})
            ).status_code == 400
            assert (
                await client.get("/health/live", headers={"X-Forwarded-Host": "admin.example.test"})
            ).status_code == 400

    asyncio.run(exercise())
