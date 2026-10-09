"""A reviewed browser decision changes only the published public projection."""

import asyncio
import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher, Type
from httpx import ASGITransport, AsyncClient
from scripts.bootstrap_test_database_roles import (
    PUBLIC_PASSWORD,
    PUBLIC_ROLE,
    PUBLICATION_PASSWORD,
    PUBLICATION_ROLE,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication import PostgresStoryReader
from primary_signal.publication.decision_writer import PublicationDecisionWriter
from primary_signal.publication.editorial_reader import EditorialReader
from primary_signal.publication.models import PublicSource, PublicStory, StoryType, Topic
from primary_signal.publication.writer import DraftReference, PublicationWriter
from primary_signal.web.admin import create_admin_app
from primary_signal.web.admin.auth import AdminAuthConfig, AdminAuthService
from primary_signal.web.admin.session_store import PostgresSessionStore
from primary_signal.web.public import create_public_app


def _decision_fields(html: str, action: str) -> dict[str, str]:
    form = re.search(rf'<form[^>]*action="[^"]*/{action}"[^>]*>(.*?)</form>', html, re.DOTALL)
    assert form is not None
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form[1]))


@pytest.mark.postgres
def test_authenticated_publish_and_suppress_reach_public_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    editorial_url = os.environ.get("PRIMARY_SIGNAL_TEST_EDITORIAL_DATABASE_URL")
    session_url = os.environ.get("PRIMARY_SIGNAL_TEST_ADMIN_SESSION_DATABASE_URL")
    decision_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DECISION_DATABASE_URL")
    if not admin_url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")
    if editorial_url is None or session_url is None or decision_url is None:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires the restricted editorial, session and decision logins")
        pytest.skip("set restricted database login DSNs")

    admin = create_engine(admin_url, hide_parameters=True)
    with admin.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    writer_engine = create_engine(
        make_url(admin_url).set(username=PUBLICATION_ROLE, password=PUBLICATION_PASSWORD),
        hide_parameters=True,
    )
    editorial = create_engine(editorial_url, hide_parameters=True)
    sessions = create_engine(session_url, hide_parameters=True)
    decisions = create_engine(decision_url, hide_parameters=True)
    public = create_engine(
        make_url(admin_url).set(username=PUBLIC_ROLE, password=PUBLIC_PASSWORD),
        hide_parameters=True,
    )
    try:
        now = datetime.now(UTC)
        source_id, article_id, article_url_id, attempt_id, content_id = (
            uuid.uuid7() for _ in range(5)
        )
        source_url = "https://public.example/synthetic-advisory"
        slug = f"synthetic-browser-flow-{uuid.uuid7().hex}"
        with admin.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO primary_signal.sources(id,source_key,name,homepage_url) "
                    "VALUES (:id,:key,'Synthetic source','https://public.example/')"
                ),
                {"id": source_id, "key": f"flow-{source_id.hex}"},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.articles(id,source_id,first_seen_at,last_seen_at) "
                    "VALUES (:id,:source,:now,:now)"
                ),
                {"id": article_id, "source": source_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.article_urls "
                    "(id,article_id,original_url,normalized_url,normalized_url_hash,"
                    "normalization_version,kind,first_seen_at,last_seen_at) "
                    "VALUES (:id,:article,:url,:url,:hash,1,'submitted',:now,:now)"
                ),
                {
                    "id": article_url_id,
                    "article": article_id,
                    "url": source_url,
                    "hash": hashlib.sha256(source_url.encode()).hexdigest(),
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.fetch_attempts "
                    "(id,article_id,retrieval_strategy,requested_url,redirect_chain,status,"
                    "started_at) VALUES (:id,:article,'direct',:url,'[]'::jsonb,'running',:now)"
                ),
                {"id": attempt_id, "article": article_id, "url": source_url, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO primary_signal.content_versions "
                    "(id,article_id,origin_fetch_attempt_id,raw_response_hash,"
                    "normalized_content_hash,normalization_version,extracted_text,"
                    "extractor_name,extractor_version,fetched_at) VALUES "
                    "(:id,:article,:attempt,:raw,:normal,1,'Synthetic evidence',"
                    "'synthetic','1',:now)"
                ),
                {
                    "id": content_id,
                    "article": article_id,
                    "attempt": attempt_id,
                    "raw": "a" * 64,
                    "normal": "b" * 64,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE primary_signal.fetch_attempts SET status='fetched',"
                    "completed_at=:now,resulting_content_version_id=:version WHERE id=:id"
                ),
                {"id": attempt_id, "now": now, "version": content_id},
            )

        source = PublicSource(
            id="synthetic-source",
            title="Synthetic source",
            publisher="Public Example",
            url=source_url,
            first_published_at=now,
            is_primary=True,
        )
        story = PublicStory(
            slug=slug,
            headline="Synthetic browser publication flow",
            synthesis="A synthetic source supports this test story.",
            why_it_matters="The public reader should only see an approved revision.",
            primary_topic=Topic.SECURITY_ENGINEERING,
            story_type=StoryType.ADVISORY,
            first_reported_at=now,
            latest_material_update_at=now,
            source_count=1,
            sources=(source,),
        )
        story_id, revision_id, fingerprint = PublicationWriter(
            writer_engine, expected_role=PUBLICATION_ROLE
        ).create_draft(story, (DraftReference(source, article_id, content_id),))

        password = "synthetic-browser-flow-password"  # noqa: S105  # pragma: allowlist secret
        password_hash = PasswordHasher(
            time_cost=2, memory_cost=8192, parallelism=1, type=Type.ID
        ).hash(password)
        auth = AdminAuthService(
            AdminAuthConfig(
                origin="https://admin.example.test",
                password_phc=password_hash,
                session_key=b"synthetic-browser-flow-session-key-32-bytes",  # pragma: allowlist secret
            ),
            PostgresSessionStore(sessions),
        )
        admin_app = create_admin_app(
            Settings(environment=RuntimeEnvironment.PRODUCTION),
            auth_service=auth,
            editorial_reader=EditorialReader(editorial, expected_role="editorial_test"),
            decision_writer=PublicationDecisionWriter(
                decisions, expected_role="publication_decision_test"
            ),
            public_origin="https://public.example.test",
        )
        public_app = create_public_app(
            Settings(environment=RuntimeEnvironment.PRODUCTION),
            story_reader=PostgresStoryReader(public),
        )

        async def exercise() -> None:
            path = f"/admin/desk/stories/{slug}"
            public_path = f"/stories/{slug}"
            async with (
                AsyncClient(
                    transport=ASGITransport(app=admin_app), base_url="https://admin.example.test"
                ) as admin_client,
                AsyncClient(
                    transport=ASGITransport(app=public_app), base_url="https://public.example.test"
                ) as public_client,
            ):
                assert (await admin_client.get(path)).status_code == 401
                assert (await public_client.get(public_path)).status_code == 404
                login = await admin_client.post(
                    "/admin/login",
                    data={"password": password},
                    headers={"Origin": "https://admin.example.test"},
                )
                assert login.status_code == 303
                desk = await admin_client.get(path)
                assert desk.status_code == 200
                assert story.headline in desk.text
                fields = _decision_fields(desk.text, "publish")
                assert fields["candidate_revision_id"] == str(revision_id)
                assert fields["evidence_fingerprint"] == fingerprint
                assert fields["expected_current_revision_id"] == ""
                publication = await admin_client.post(
                    f"{path}/publish",
                    data={**fields, "reason": "Reviewed synthetic source and public URL"},
                    headers={"Origin": "https://admin.example.test"},
                )
                assert publication.status_code == 303
                published = await public_client.get(public_path)
                assert published.status_code == 200
                assert story.headline in published.text
                assert source_url in published.text
                assert "Synthetic evidence" not in published.text

                updated_desk = await admin_client.get(path)
                assert updated_desk.status_code == 200
                suppress_fields = _decision_fields(updated_desk.text, "suppress")
                assert suppress_fields["expected_current_revision_id"] == str(revision_id)
                suppression = await admin_client.post(
                    f"{path}/suppress",
                    data={**suppress_fields, "reason": "Synthetic suppression review"},
                    headers={"Origin": "https://admin.example.test"},
                )
                assert suppression.status_code == 303
                assert (await public_client.get(public_path)).status_code == 404
                assert slug not in (await public_client.get("/")).text

        asyncio.run(exercise())
        with admin.connect() as connection:
            assert connection.execute(
                text("SELECT suppressed FROM primary_signal.stories WHERE id=:id"),
                {"id": story_id},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT to_status,actor,reason FROM primary_signal.publication_events "
                    "WHERE story_id=:id ORDER BY occurred_at,id"
                ),
                {"id": story_id},
            ).all() == [
                ("draft", "system", "draft created"),
                ("validated", "site operator", "Reviewed synthetic source and public URL"),
                ("published", "site operator", "Reviewed synthetic source and public URL"),
                ("suppressed", "site operator", "Synthetic suppression review"),
            ]
    finally:
        for engine in (public, decisions, sessions, editorial, writer_engine, admin):
            engine.dispose()
