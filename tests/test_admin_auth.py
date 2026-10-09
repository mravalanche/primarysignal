"""Authentication boundary tests with synthetic, disposable credentials."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher, Type
from httpx import ASGITransport, AsyncClient

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.admin import create_admin_app
from primary_signal.web.admin.auth import AdminAuthConfig, AdminAuthService, StoredSession
from primary_signal.web.admin.settings import AdminAuthSettings

ORIGIN = "https://admin.example.test"
PASSWORD = "synthetic-password-for-unit-tests"  # noqa: S105  # pragma: allowlist secret


class FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[bytes, StoredSession] = {}
        self.attempts: list[datetime] = []

    def claim_login_attempt(
        self,
        *,
        since: datetime,
        at: datetime,
        limit: int,
    ) -> bool:
        recent = [item for item in self.attempts if item >= since]
        if len(recent) >= limit:
            return False
        self.attempts.append(at)
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
        login_attempts=4,
    )
    return AdminAuthService(config, store or FakeStore())


def test_production_admin_requires_authentication_config() -> None:
    with pytest.raises(RuntimeError, match="requires configured authentication"):
        create_admin_app(Settings(environment=RuntimeEnvironment.PRODUCTION))


def test_private_settings_validate_origin_and_secret_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hasher = PasswordHasher(time_cost=2, memory_cost=8192, parallelism=1, type=Type.ID)
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_ORIGIN", ORIGIN)
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC", hasher.hash(PASSWORD))
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_SESSION_KEY_HEX", (b"k" * 32).hex())
    settings = AdminAuthSettings()  # type: ignore[call-arg]
    assert settings.auth_config().host == "admin.example.test"
    assert PASSWORD not in repr(settings)
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_SESSION_KEY_HEX", "not-hex")
    with pytest.raises(ValueError, match="hexadecimal"):
        AdminAuthSettings().auth_config()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="HTTPS origin"):
        AdminAuthConfig("http://admin.example.test", hasher.hash(PASSWORD), b"k" * 32)
    with pytest.raises(ValueError, match="Argon2id"):
        AdminAuthConfig(ORIGIN, "plain", b"k" * 32)
    with pytest.raises(ValueError, match="32 bytes"):
        AdminAuthConfig(ORIGIN, hasher.hash(PASSWORD), b"short")
    with pytest.raises(ValueError, match="origin must not contain credentials"):
        AdminAuthConfig(
            "https://user:pass@admin.example.test",  # pragma: allowlist secret
            hasher.hash(PASSWORD),
            b"k" * 32,
        )
    with pytest.raises(ValueError, match="lifetimes"):
        AdminAuthConfig(
            ORIGIN, hasher.hash(PASSWORD), b"k" * 32, idle_seconds=60, absolute_seconds=30
        )
    with pytest.raises(ValueError, match="rate limits"):
        AdminAuthConfig(ORIGIN, hasher.hash(PASSWORD), b"k" * 32, login_attempts=0)


def test_session_rotation_csrf_expiry_and_rate_limits() -> None:
    store = FakeStore()
    auth = auth_service(store)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    first = auth.login(PASSWORD, now=now)
    assert first is not None
    assert len(first.identifier) == 64
    assert first.identifier.encode() not in repr(store.sessions).encode()
    session = auth.authenticate(first.identifier, now=now + timedelta(minutes=1))
    assert session is not None
    assert auth.valid_csrf(session, first.csrf_token)
    assert not auth.valid_csrf(session, "00" * 32)
    assert not auth.valid_csrf(session, "not-hex")
    assert (
        auth_service(store).authenticate(first.identifier, now=now + timedelta(minutes=1)) is None
    )
    assert auth.login("wrong", now=now) is None
    assert auth.login("wrong", now=now) is None
    assert auth.login("wrong", now=now) is None
    from primary_signal.web.admin.auth import LoginRateLimited

    try:
        auth.login(PASSWORD, now=now)
    except LoginRateLimited:
        pass
    else:
        raise AssertionError("service attempt threshold was bypassed")
    assert auth.authenticate(first.identifier, now=now + timedelta(minutes=31)) is None
    digest = next(iter(store.sessions))
    store.sessions[digest] = StoredSession(
        digest, session.csrf_secret, now, now + timedelta(hours=11, minutes=59)
    )
    assert auth.authenticate(first.identifier, now=now + timedelta(hours=12, seconds=1)) is None
    assert auth.authenticate("malformed", now=now) is None
    assert auth.authenticate("00", now=now) is None
    auth.logout(first.identifier)
    assert auth.authenticate(first.identifier, now=now + timedelta(minutes=1)) is None


def test_service_budget_expires_without_permanent_lockout() -> None:
    store = FakeStore()
    auth = auth_service(store)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    for _ in range(4):
        assert auth.login("wrong", now=now) is None
    from primary_signal.web.admin.auth import LoginRateLimited

    with pytest.raises(LoginRateLimited):
        auth.login(PASSWORD, now=now)
    assert auth.login(PASSWORD, now=now + timedelta(minutes=16)) is not None


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
            for payload in (b"{", b"[]", b'{"password":42}', b'{"password":"x","extra":1}'):
                invalid = await client.post(
                    "/admin/auth/login",
                    content=payload,
                    headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                )
                assert invalid.status_code == 400
                assert PASSWORD not in invalid.text
            wrong_type = await client.post(
                "/admin/auth/login",
                content=b'{"password":"x"}',
                headers={"Origin": ORIGIN, "Content-Type": "text/plain"},
            )
            assert wrong_type.status_code == 400
            wrong_password = await client.post(
                "/admin/auth/login",
                json={"password": "wrong"},  # pragma: allowlist secret
                headers={"Origin": ORIGIN},
            )
            assert wrong_password.status_code == 401
            assert "wrong" not in wrong_password.text
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
            original_cookie = login.cookies.get("__Host-primary_signal_admin")
            second_login = await client.post(
                "/admin/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert second_login.status_code == 200
            assert original_cookie is not None
            assert auth.authenticate(original_cookie) is None
            login = second_login
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
