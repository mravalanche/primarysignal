"""Grant the feed poll worker access to poll and discovery records."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261007_05"
down_revision: str | Sequence[str] | None = "20261001_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Grant column-specific ingestion access to a dedicated capability."""

    role = "primary_signal_cap_feed_poll"
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {role}")
    op.execute(
        "GRANT SELECT (id, source_id, configured_url, normalized_url, enabled, etag, last_modified, "
        "consecutive_failures) "
        f"ON primary_signal.feeds TO {role}"
    )
    op.execute(
        f"GRANT SELECT (id, enabled) ON primary_signal.sources TO {role}"
    )
    op.execute(
        "GRANT UPDATE (etag, last_modified, last_attempt_at, last_success_at, "
        f"consecutive_failures, updated_at) ON primary_signal.feeds TO {role}"
    )
    op.execute(
        "GRANT INSERT (id, feed_id, job_id, requested_url, status, started_at, "
        "completed_at, error_code) "
        f"ON primary_signal.feed_poll_runs TO {role}"
    )
    op.execute(
        "GRANT SELECT (id) "
        f"ON primary_signal.feed_poll_runs TO {role}"
    )
    op.execute(
        "GRANT UPDATE (status, completed_at, http_status, entries_seen, "
        "entries_discovered, returned_etag, returned_last_modified, error_code, "
        f"error_detail) ON primary_signal.feed_poll_runs TO {role}"
    )
    op.execute(
        "GRANT INSERT (id, feed_id, first_poll_run_id, identity_method, "
        "identity_version, identity_key, reported_guid, reported_url, "
        "reported_title, reported_summary, reported_author, reported_published_at, "
        "reported_updated_at, metadata_hash, first_seen_at, last_seen_at) "
        f"ON primary_signal.feed_entries TO {role}"
    )
    op.execute(
        "GRANT SELECT (id, feed_id, identity_version, identity_key, article_id, last_seen_at) "
        f"ON primary_signal.feed_entries TO {role}"
    )
    op.execute(
        "GRANT UPDATE (article_id, last_seen_at) "
        f"ON primary_signal.feed_entries TO {role}"
    )
    op.execute(
        "GRANT INSERT (id, source_id, first_seen_at, "
        f"last_seen_at) ON primary_signal.articles TO {role}"
    )
    op.execute(
        f"GRANT SELECT (id, last_seen_at) ON primary_signal.articles TO {role}"
    )
    op.execute(
        "GRANT UPDATE (current_canonical_url_id, last_seen_at) "
        f"ON primary_signal.articles TO {role}"
    )
    op.execute(
        "GRANT INSERT (id, article_id, original_url, normalized_url, "
        "normalized_url_hash, normalization_version, kind, first_seen_at, "
        f"last_seen_at) ON primary_signal.article_urls TO {role}"
    )
    op.execute(
        "GRANT SELECT (id, article_id, normalized_url_hash, normalization_version, "
        "last_seen_at) "
        f"ON primary_signal.article_urls TO {role}"
    )
    op.execute(
        "GRANT UPDATE (last_seen_at) ON primary_signal.article_urls "
        f"TO {role}"
    )


def downgrade() -> None:
    """Remove ingestion capability grants without changing other roles."""

    role = "primary_signal_cap_feed_poll"
    for table in (
        "article_urls",
        "articles",
        "feed_entries",
        "feed_poll_runs",
        "feeds",
        "sources",
    ):
        op.execute(f"REVOKE ALL ON primary_signal.{table} FROM {role}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {role}")
