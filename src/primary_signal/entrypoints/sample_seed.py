"""Seed one fictional draft with complete provenance in a disposable test database."""

import hashlib
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, text

from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.db.engine import assert_database_role
from primary_signal.publication.models import PublicSource, PublicStory, StoryType, Topic
from primary_signal.publication.writer import DraftReference, PublicationWriter

NAMESPACE = uuid.UUID("0f5969a6-31a9-4f49-af77-a4d188ebbc40")
STAMP = datetime(2026, 1, 1, 12, tzinfo=UTC)
SLUG = "synthetic-security-notice"
URL = "https://public.example/synthetic-security-notice"
SOURCE_ID = uuid.uuid5(NAMESPACE, "source")
ARTICLE_ID = uuid.uuid5(NAMESPACE, "article")
URL_ID = uuid.uuid5(NAMESPACE, "article-url")
ATTEMPT_ID = uuid.uuid5(NAMESPACE, "fetch-attempt")
VERSION_ID = uuid.uuid5(NAMESPACE, "content-version")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _settings(prefix: str) -> DatabaseSettings:
    url = os.environ.get(f"{prefix}_DATABASE_URL")
    role = os.environ.get(f"{prefix}_DATABASE_EXPECTED_ROLE")
    if not url or not role:
        raise ValueError("both disposable database connections and expected roles are required")
    return DatabaseSettings(url=url, expected_role=role, application_name="sample_seed")


def assert_test_database(connection: Connection, role: str) -> None:
    assert_database_role(connection, role)
    name = connection.execute(text("SELECT current_database()")).scalar_one()
    if not isinstance(name, str) or not name.endswith("_test"):
        raise RuntimeError("sample seed requires a disposable _test database")


def _insert_lineage(connection: Connection) -> None:
    """Use stable identities; reject a collision instead of modifying old evidence."""
    statements = (
        (
            "sources",
            "id,source_key,name,homepage_url",
            ":id,'synthetic-sample','Synthetic source','https://public.example/'",
            {"id": SOURCE_ID},
        ),
        (
            "articles",
            "id,source_id,first_seen_at,last_seen_at",
            ":id,:source,:stamp,:stamp",
            {"id": ARTICLE_ID, "source": SOURCE_ID, "stamp": STAMP},
        ),
        (
            "article_urls",
            "id,article_id,original_url,normalized_url,normalized_url_hash,normalization_version,kind,first_seen_at,last_seen_at",
            ":id,:article,:url,:url,:hash,1,'submitted',:stamp,:stamp",
            {"id": URL_ID, "article": ARTICLE_ID, "url": URL, "hash": _sha(URL), "stamp": STAMP},
        ),
        (
            "fetch_attempts",
            "id,article_id,retrieval_strategy,requested_url,redirect_chain,status,started_at",
            ":id,:article,'synthetic',:url,'[]'::jsonb,'running',:stamp",
            {"id": ATTEMPT_ID, "article": ARTICLE_ID, "url": URL, "stamp": STAMP},
        ),
        (
            "content_versions",
            "id,article_id,origin_fetch_attempt_id,raw_response_hash,normalized_content_hash,normalization_version,extracted_title,extracted_text,extractor_name,extractor_version,fetched_at",
            ":id,:article,:attempt,:raw,:normal,1,'Synthetic security notice','Fictional evidence for a disposable publication test.','synthetic','1',:stamp",
            {
                "id": VERSION_ID,
                "article": ARTICLE_ID,
                "attempt": ATTEMPT_ID,
                "raw": _sha("synthetic raw response"),
                "normal": _sha("synthetic normalized content"),
                "stamp": STAMP,
            },
        ),
    )
    for table, columns, values, params in statements:
        connection.execute(
            text(
                f"INSERT INTO primary_signal.{table} ({columns}) VALUES ({values}) ON CONFLICT (id) DO NOTHING"  # noqa: S608  # Static statements above.
            ),
            params,
        )
    connection.execute(
        text(
            "UPDATE primary_signal.fetch_attempts SET status='fetched', "
            "completed_at=:stamp, resulting_content_version_id=:version "
            "WHERE id=:attempt AND status='running'"
        ),
        {"stamp": STAMP, "version": VERSION_ID, "attempt": ATTEMPT_ID},
    )
    row = (
        connection.execute(
            text(
                "SELECT s.source_key, s.homepage_url, a.source_id, "
                "u.article_id AS url_article, u.normalized_url, u.normalized_url_hash, "
                "f.article_id AS fetch_article, f.requested_url, f.status, "
                "f.resulting_content_version_id, v.article_id AS version_article, "
                "v.origin_fetch_attempt_id, v.normalized_content_hash "
                "FROM primary_signal.sources s, primary_signal.articles a, "
                "primary_signal.article_urls u, primary_signal.fetch_attempts f, "
                "primary_signal.content_versions v "
                "WHERE s.id=:source AND a.id=:article AND u.id=:url "
                "AND f.id=:attempt AND v.id=:version"
            ),
            {
                "source": SOURCE_ID,
                "article": ARTICLE_ID,
                "url": URL_ID,
                "attempt": ATTEMPT_ID,
                "version": VERSION_ID,
            },
        )
        .mappings()
        .one()
    )
    if (
        row["source_key"],
        row["homepage_url"],
        row["source_id"],
        row["url_article"],
        row["normalized_url"],
        row["normalized_url_hash"],
        row["fetch_article"],
        row["requested_url"],
        row["status"],
        row["resulting_content_version_id"],
        row["version_article"],
        row["origin_fetch_attempt_id"],
        row["normalized_content_hash"],
    ) != (
        "synthetic-sample",
        "https://public.example/",
        SOURCE_ID,
        ARTICLE_ID,
        URL,
        _sha(URL),
        ARTICLE_ID,
        URL,
        "fetched",
        VERSION_ID,
        ARTICLE_ID,
        ATTEMPT_ID,
        _sha("synthetic normalized content"),
    ):
        raise RuntimeError("synthetic lineage identity conflicts with existing data")


def seed(
    lineage_engine: Engine, writer_engine: Engine, *, lineage_role: str, writer_role: str
) -> bool:
    """Return true when a draft is created and false for a verified rerun."""
    if lineage_role == writer_role:
        raise ValueError("lineage and publication writer must use separate logins")
    writer = PublicationWriter(writer_engine, expected_role=writer_role)
    source = PublicSource(
        "synthetic-source", "Synthetic security notice", "Public Example", URL, STAMP, True
    )
    story = PublicStory(
        slug=SLUG,
        headline="Synthetic security notice",
        synthesis="A fictional notice used to test publication.",
        why_it_matters="It exercises the public reading path with traceable test data.",
        primary_topic=Topic.SECURITY_ENGINEERING,
        story_type=StoryType.ADVISORY,
        first_reported_at=STAMP,
        latest_material_update_at=STAMP,
        source_count=1,
        sources=(source,),
    )
    with lineage_engine.begin() as connection:
        assert_test_database(connection, lineage_role)
        # The same disposable database must be used by both isolated logins.
        with writer_engine.connect() as writer_connection:
            assert_test_database(writer_connection, writer_role)
            if (
                connection.execute(text("SELECT current_database()")).scalar_one()
                != writer_connection.execute(text("SELECT current_database()")).scalar_one()
            ):
                raise RuntimeError("sample seed logins must use the same disposable database")
        connection.execute(text("SELECT pg_advisory_xact_lock(20885434, 1)"))
        _insert_lineage(connection)
    with lineage_engine.begin() as connection:
        assert_test_database(connection, lineage_role)
        connection.execute(text("SELECT pg_advisory_xact_lock(20885434, 1)"))
        existing = (
            connection.execute(
                text(
                    "SELECT r.headline, rs.article_id, rs.content_version_id FROM primary_signal.stories s "
                    "JOIN primary_signal.story_revisions r ON r.story_id=s.id "
                    "JOIN primary_signal.revision_sources rs ON rs.revision_id=r.id "
                    "WHERE s.slug=:slug ORDER BY r.revision_number LIMIT 1"
                ),
                {"slug": SLUG},
            )
            .mappings()
            .first()
        )
        if existing:
            if (existing["headline"], existing["article_id"], existing["content_version_id"]) != (
                story.headline,
                ARTICLE_ID,
                VERSION_ID,
            ):
                raise RuntimeError("synthetic draft conflicts with existing data")
            return False
        writer.create_draft(story, (DraftReference(source, ARTICLE_ID, VERSION_ID),))
        return True


def main() -> None:
    """Run only against explicitly configured disposable PostgreSQL roles."""
    lineage_engine = writer_engine = None
    try:
        lineage = _settings("PRIMARY_SIGNAL_TEST")
        writer = _settings("PRIMARY_SIGNAL_TEST_PUBLICATION")
        lineage_engine = create_database_engine(lineage)
        writer_engine = create_database_engine(writer)
        created = seed(
            lineage_engine,
            writer_engine,
            lineage_role=lineage.expected_role,
            writer_role=writer.expected_role,
        )
    except Exception:
        raise SystemExit(
            "synthetic seed failed; verify disposable database configuration"
        ) from None
    finally:
        if writer_engine is not None:
            writer_engine.dispose()
        if lineage_engine is not None:
            lineage_engine.dispose()
    print("synthetic draft created" if created else "synthetic draft already present")


if __name__ == "__main__":  # pragma: no cover
    main()
