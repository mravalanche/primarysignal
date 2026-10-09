"""Grant private publication metadata to a dedicated read-only capability."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_12"
down_revision: str | Sequence[str] | None = "20261009_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_editorial_read"
GRANTS = {
    "stories": "id, slug, current_revision_id, suppressed, created_at",
    "story_revisions": (
        "id, story_id, revision_number, status, headline, synthesis, why_it_matters, "
        "primary_topic, story_type, first_reported_at, latest_material_update_at, "
        "published_at, created_at"
    ),
    "revision_sources": (
        "revision_id, source_id, position, title, publisher, public_url, "
        "first_published_at, is_primary, article_id, content_version_id"
    ),
    "publication_events": (
        "id, story_id, revision_id, from_status, to_status, actor, reason, "
        "input_fingerprint, occurred_at"
    ),
    "articles": "id, source_id, current_canonical_url_id",
    "content_versions": "id, article_id, normalized_content_hash, fetched_at, word_count",
    "sources": "id, source_key, name, enabled",
    "article_urls": "id, article_id, original_url, normalized_url",
}


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    for table, columns in GRANTS.items():
        op.execute(f"GRANT SELECT ({columns}) ON primary_signal.{table} TO {ROLE}")


def downgrade() -> None:
    for table, columns in reversed(tuple(GRANTS.items())):
        op.execute(f"REVOKE SELECT ({columns}) ON primary_signal.{table} FROM {ROLE}")  # noqa: S608
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
