"""Append-only publication decisions and a restricted writer capability."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_09"
down_revision: str | Sequence[str] | None = "20261009_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_publication_write"


def upgrade() -> None:
    op.execute("""
CREATE TABLE primary_signal.publication_events (
    id uuid PRIMARY KEY,
    story_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    from_status text,
    to_status text NOT NULL CHECK (to_status IN
        ('draft','validated','published','suppressed','superseded')),
    actor text NOT NULL CHECK (char_length(actor) BETWEEN 1 AND 160),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 1024),
    input_fingerprint text CHECK (input_fingerprint IS NULL OR
        input_fingerprint ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (story_id, revision_id) REFERENCES
        primary_signal.story_revisions(story_id,id) ON DELETE RESTRICT
)
""")
    op.execute(
        "CREATE INDEX ix_publication_events_revision_time ON "
        "primary_signal.publication_events(revision_id,occurred_at)"
    )
    op.execute(
        "CREATE TRIGGER guard_publication_events BEFORE UPDATE OR DELETE "
        "ON primary_signal.publication_events FOR EACH ROW EXECUTE FUNCTION "
        "primary_signal.reject_immutable_history_change()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    for table, columns in {
        "stories": "id, slug, current_revision_id, suppressed",
        "story_revisions": "id, story_id, revision_number, status",
        "revision_sources": "revision_id, article_id, content_version_id",
        "content_versions": "id, article_id, origin_fetch_attempt_id",
        "fetch_attempts": "id, article_id, status, resulting_content_version_id",
        "articles": "id, source_id",
        "sources": "id, enabled",
        "publication_events": "revision_id, to_status, input_fingerprint, occurred_at",
        # guard_story_revision() is SECURITY INVOKER and reads these exact
        # metadata columns while checking every published signal's evidence.
        # The writer receives no source URL or article-body access here.
        "revision_signals": "revision_id, kind",
        "signal_evidence": "revision_id, kind",
    }.items():
        op.execute(f"GRANT SELECT ({columns}) ON primary_signal.{table} TO {ROLE}")
    for table, columns in {
        "stories": "id, slug",
        "story_revisions": (
            "id, story_id, revision_number, headline, synthesis, why_it_matters, "
            "primary_topic, story_type, uk_relevant, first_reported_at, "
            "latest_material_update_at"
        ),
        "revision_sources": (
            "revision_id, source_id, position, title, publisher, public_url, "
            "first_published_at, is_primary, article_id, content_version_id"
        ),
        "publication_events": (
            "id, story_id, revision_id, from_status, to_status, actor, reason, input_fingerprint"
        ),
    }.items():
        op.execute(f"GRANT INSERT ({columns}) ON primary_signal.{table} TO {ROLE}")
    op.execute(f"GRANT UPDATE (current_revision_id,suppressed) ON primary_signal.stories TO {ROLE}")
    op.execute(f"GRANT UPDATE (status,published_at) ON primary_signal.story_revisions TO {ROLE}")


def downgrade() -> None:
    for table in (
        "publication_events",
        "revision_sources",
        "story_revisions",
        "stories",
        "revision_signals",
        "signal_evidence",
        "content_versions",
        "fetch_attempts",
        "articles",
        "sources",
    ):
        op.execute(f"REVOKE ALL ON primary_signal.{table} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
    op.execute("DROP TABLE primary_signal.publication_events")
