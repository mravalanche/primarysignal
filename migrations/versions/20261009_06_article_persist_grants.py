"""Grant extracted article persistence to a dedicated capability."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_06"
down_revision: str | Sequence[str] | None = "20261007_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Grant only target reads and article observation writes."""

    role = "primary_signal_cap_article_persist"
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {role}")
    op.execute(f"GRANT SELECT (id, enabled) ON primary_signal.sources TO {role}")
    op.execute(
        "GRANT SELECT (id, source_id, current_canonical_url_id) "
        f"ON primary_signal.articles TO {role}"
    )
    op.execute(f"GRANT UPDATE (current_content_version_id) ON primary_signal.articles TO {role}")
    op.execute(
        f"GRANT SELECT (id, article_id, normalized_url) ON primary_signal.article_urls TO {role}"
    )
    op.execute(
        "GRANT INSERT (id, article_id, job_id, retrieval_strategy, requested_url, "
        "final_url, redirect_chain, status, started_at) "
        f"ON primary_signal.fetch_attempts TO {role}"
    )
    op.execute(f"GRANT SELECT (id) ON primary_signal.fetch_attempts TO {role}")
    op.execute(
        "GRANT UPDATE (status, completed_at, http_status, content_type, byte_count, "
        "returned_etag, returned_last_modified, resulting_content_version_id, error_code) "
        f"ON primary_signal.fetch_attempts TO {role}"
    )
    op.execute(
        "GRANT INSERT (id, article_id, origin_fetch_attempt_id, raw_response_hash, "
        "normalized_content_hash, normalization_version, extracted_title, extracted_text, "
        "extractor_name, extractor_version, content_type, word_count, fetched_at) "
        f"ON primary_signal.content_versions TO {role}"
    )
    op.execute(
        "GRANT SELECT (id, article_id, normalization_version, normalized_content_hash) "
        f"ON primary_signal.content_versions TO {role}"
    )


def downgrade() -> None:
    """Remove only the dedicated article persistence capability."""

    role = "primary_signal_cap_article_persist"
    for table in (
        "content_versions",
        "fetch_attempts",
        "article_urls",
        "articles",
        "sources",
    ):
        op.execute(f"REVOKE ALL ON primary_signal.{table} FROM {role}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {role}")
