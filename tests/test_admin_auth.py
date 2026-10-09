"""Authentication boundary tests with synthetic, disposable credentials."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from argon2 import PasswordHasher, Type
from httpx import ASGITransport, AsyncClient

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication.editorial_reader import (
    EditorialRevision,
    EditorialSourceLineage,
    EditorialStoryDetail,
    EditorialStoryPage,
    EditorialStoryRow,
)
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
    with pytest.raises(RuntimeError, match="restricted editorial reader"):
        create_admin_app(
            Settings(environment=RuntimeEnvironment.PRODUCTION), auth_service=auth_service()
        )
    with pytest.raises(ValueError, match="public origin"):
        create_admin_app(
            Settings(environment=RuntimeEnvironment.TEST),
            public_origin="https://user:pass@public.example",  # pragma: allowlist secret
        )
    with pytest.raises(ValueError, match="different hostnames"):
        create_admin_app(
            Settings(environment=RuntimeEnvironment.TEST),
            auth_service=auth_service(),
            public_origin=f"{ORIGIN}:8443",
        )


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


def test_browser_login_form_uses_same_boundary_and_rate_limit() -> None:
    auth = auth_service()
    app = create_admin_app(Settings(environment=RuntimeEnvironment.TEST), auth_service=auth)

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            page = await client.get("/admin/login")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert '<form action="/admin/login" method="post"' in page.text
            assert PASSWORD not in page.text
            assert (await client.get("/assets/admin/admin.css")).status_code == 200
            assert (await client.get("/assets/common/htmx.min.js")).status_code == 200
            assert (await client.get("/assets/admin/private.css")).status_code == 401
            assert (
                await client.post("/admin/login", data={"password": PASSWORD})
            ).status_code == 403
            for payload in ("password=", "password=x&extra=y", "password=%FF"):
                invalid = await client.post(
                    "/admin/login",
                    content=payload,
                    headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
                )
                assert invalid.status_code == 400
            assert (
                await client.post("/admin/login", content=b"password=x", headers={"Origin": ORIGIN})
            ).status_code == 400
            assert (
                await client.post(
                    "/admin/login",
                    content=b"password=" + b"x" * 8193,
                    headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
                )
            ).status_code == 400
            assert (
                await client.post(
                    "/admin/login",
                    content=b"password=x&password=y",
                    headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
                )
            ).status_code == 400
            wrong = await client.post(
                "/admin/login",
                data={"password": "wrong"},  # pragma: allowlist secret
                headers={"Origin": ORIGIN},
            )
            assert wrong.status_code == 401
            assert "wrong" not in wrong.text.lower()
            signed_in = await client.post(
                "/admin/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert signed_in.status_code == 303
            assert signed_in.headers["location"] == "/admin/desk"
            assert signed_in.headers["cache-control"] == "no-store"
            assert "Secure" in signed_in.headers["set-cookie"]
            assert (await client.get("/admin/auth/session")).status_code == 200

    asyncio.run(exercise())


def test_browser_login_form_observes_service_attempt_budget() -> None:
    app = create_admin_app(
        Settings(environment=RuntimeEnvironment.TEST), auth_service=auth_service()
    )

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            for _ in range(4):
                response = await client.post(
                    "/admin/login",
                    data={"password": "wrong"},  # pragma: allowlist secret
                    headers={"Origin": ORIGIN},
                )
                assert response.status_code == 401
            limited = await client.post(
                "/admin/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert limited.status_code == 429
            assert limited.headers["cache-control"] == "no-store"
            assert PASSWORD not in limited.text

    asyncio.run(exercise())


def test_live_reading_desk_requires_session_and_renders_untrusted_metadata_inert() -> None:
    auth = auth_service()
    reader = MagicMock()
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    story_id, revision_id = uuid.uuid7(), uuid.uuid7()
    row = EditorialStoryRow(
        story_id=story_id,
        slug="synthetic-story",
        current_revision_id=None,
        suppressed=False,
        created_at=now,
        candidate_revision_id=revision_id,
        candidate_number=1,
        candidate_status="draft",
        candidate_headline="<script>alert(1)</script>",
        candidate_topic="security-engineering",
        candidate_updated_at=now,
    )
    revision = EditorialRevision(
        id=revision_id,
        number=1,
        status="draft",
        headline=row.candidate_headline or "",
        synthesis="A synthetic source was reviewed.",
        why_it_matters="The evidence is visible to an editor.",
        primary_topic="security-engineering",
        story_type="advisory",
        first_reported_at=now,
        latest_material_update_at=now,
        published_at=None,
        created_at=now,
    )
    source = EditorialSourceLineage(
        source_id="source-1",
        position=1,
        title="Untrusted <img src=x onerror=alert(1)>",
        publisher="Synthetic publisher",
        public_url="javascript:alert(1)",
        first_published_at=None,
        is_primary=True,
        article_id=None,
        content_version_id=None,
        content_hash=None,
        fetched_at=None,
        configured_source_key=None,
        configured_source_name=None,
        configured_source_enabled=None,
        current_canonical_url=None,
        public_url_belongs_to_article=False,
    )
    reader.list_stories.return_value = EditorialStoryPage(items=(row,), next_position=None)
    reader.get_story.return_value = EditorialStoryDetail(
        story=row,
        revisions=(revision,),
        revisions_have_more=False,
        current_revision=None,
        candidate_sources=(source,),
        sources_have_more=False,
        current_sources=(),
        current_sources_have_more=False,
        events=(),
        events_have_more=False,
    )
    app = create_admin_app(
        Settings(environment=RuntimeEnvironment.TEST), auth_service=auth, editorial_reader=reader
    )

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            denied = await client.get("/admin/desk")
            assert denied.status_code == 401
            reader.list_stories.assert_not_called()
            login = await client.post(
                "/admin/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert login.status_code == 303
            page = await client.get("/admin/desk/stories/synthetic-story")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert page.headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
            assert "<script>alert(1)</script>" not in page.text
            assert "javascript:alert(1)" not in page.text
            assert "URL unavailable" in page.text
            assert "No current validation result" in page.text
            assert "Ready to publish" not in page.text
            assert 'name="csrf_token"' in page.text
            assert (
                await client.post("/admin/logout", headers={"Origin": ORIGIN})
            ).status_code == 400
            token = auth.authenticate(login.cookies["__Host-primary_signal_admin"])
            assert token is not None
            assert (
                await client.post(
                    "/admin/logout", data={"csrf_token": "00" * 32}, headers={"Origin": ORIGIN}
                )
            ).status_code == 403
            assert (
                await client.post("/admin/logout", data={"csrf_token": token.csrf_secret.hex()})
            ).status_code == 403
            assert (
                await client.post(
                    "/admin/logout",
                    content=b"csrf_token=" + b"x" * 257,
                    headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
                )
            ).status_code == 400
            logout = await client.post(
                "/admin/logout",
                data={"csrf_token": token.csrf_secret.hex()},
                headers={"Origin": ORIGIN},
            )
            assert logout.status_code == 303
            assert (await client.get("/admin/desk")).status_code == 401

    asyncio.run(exercise())


def test_reading_desk_filters_and_cursor_are_bounded_and_linkable() -> None:
    auth = auth_service()
    reader = MagicMock()
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    row = EditorialStoryRow(
        story_id=uuid.uuid7(),
        slug="synthetic-story",
        current_revision_id=None,
        suppressed=False,
        created_at=now,
        candidate_revision_id=None,
        candidate_number=None,
        candidate_status=None,
        candidate_headline=None,
        candidate_topic=None,
        candidate_updated_at=None,
    )
    reader.list_stories.return_value = EditorialStoryPage(
        items=(row,), next_position=(row.created_at, row.story_id)
    )
    app = create_admin_app(
        Settings(environment=RuntimeEnvironment.TEST), auth_service=auth, editorial_reader=reader
    )

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            assert (await client.get("/admin/desk?state=ready")).status_code == 401
            reader.list_stories.assert_not_called()
            await client.post(
                "/admin/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert (await client.get("/admin/desk?state=ready")).status_code == 422
            assert (await client.get("/admin/desk?before_at=2026-10-09")).status_code == 400
            assert (
                await client.get("/admin/desk?before_at=2026-10-09&before_id=bad")
            ).status_code == 400
            assert (await client.get("/admin/desk?source=../private")).status_code == 422
            reader.list_stories.assert_not_called()
            page = await client.get(
                "/admin/desk?q=synthetic&state=draft&topic=security-engineering&source=vendor"
            )
            assert page.status_code == 200
            assert "Older stories" in page.text
            assert "before_at=" in page.text and "before_id=" in page.text
            assert "q=synthetic" in page.text and "source=vendor" in page.text
            assert "No revision yet" in page.text
            reader.list_stories.assert_called_once_with(
                limit=20,
                before=None,
                q="synthetic",
                state="draft",
                topic="security-engineering",
                source="vendor",
            )
            cursor = urlencode({"before_at": now.isoformat(), "before_id": str(row.story_id)})
            older = await client.get(f"/admin/desk?{cursor}")
            assert older.status_code == 200
            assert reader.list_stories.call_args.kwargs["before"] == (now, row.story_id)

    asyncio.run(exercise())
