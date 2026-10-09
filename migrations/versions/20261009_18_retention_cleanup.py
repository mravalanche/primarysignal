"""Clear only old, unreferenced extracted text through a bounded capability."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_18"
down_revision: str | Sequence[str] | None = "20261009_17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_retention_cleanup"
FUNCTION = "primary_signal.clear_expired_extracted_text(integer)"


def upgrade() -> None:
    # Use the same canonical historical-publication marker as the inventory.
    # A suppressed or superseded published revision retains published_at.
    op.execute("""
CREATE OR REPLACE FUNCTION primary_signal.guard_content_version_text_clear()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'content version history only permits text clearing'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.extracted_text IS NULL
       OR NEW.extracted_text IS NOT NULL
       OR (to_jsonb(NEW) - 'extracted_text')
          IS DISTINCT FROM (to_jsonb(OLD) - 'extracted_text') THEN
        RAISE EXCEPTION 'content version history only permits text clearing'
            USING ERRCODE = 'P0001';
    END IF;
    PERFORM 1 FROM primary_signal.articles
    WHERE id = OLD.article_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM primary_signal.articles
        WHERE current_content_version_id = OLD.id
    ) OR EXISTS (
        SELECT 1 FROM primary_signal.revision_sources AS relation
        JOIN primary_signal.story_revisions AS revision
          ON revision.id = relation.revision_id
        WHERE relation.article_id = OLD.article_id
          AND relation.content_version_id = OLD.id
          AND revision.published_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'current or published content text must be retained'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
""")
    op.execute("""
CREATE FUNCTION primary_signal.clear_expired_extracted_text(p_batch_limit integer)
RETURNS SETOF uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
SET TimeZone = 'UTC'
AS $retention_cleanup$
DECLARE
    candidate record;
    locked_id uuid;
BEGIN
    IF p_batch_limit IS NULL OR p_batch_limit < 1 OR p_batch_limit > 500 THEN
        RAISE EXCEPTION 'retention batch limit must be between 1 and 500'
            USING ERRCODE = '22023';
    END IF;

    -- The initial scan is advisory. Every condition is checked again below
    -- after taking article then version row locks in the same transaction.
    FOR candidate IN
        SELECT version.article_id, version.id AS version_id
        FROM primary_signal.content_versions AS version
        JOIN primary_signal.content_version_retention AS retention
          ON retention.content_version_id = version.id
        JOIN primary_signal.articles AS article ON article.id = version.article_id
        WHERE version.extracted_text IS NOT NULL
          AND article.current_content_version_id IS DISTINCT FROM version.id
          AND retention.superseded_at <= clock_timestamp() - interval '90 days'
          AND NOT EXISTS (
              SELECT 1 FROM primary_signal.revision_sources AS relation
              JOIN primary_signal.story_revisions AS revision
                ON revision.id = relation.revision_id
              WHERE relation.article_id = version.article_id
                AND relation.content_version_id = version.id
                AND revision.published_at IS NOT NULL
          )
        ORDER BY retention.superseded_at, version.id
        LIMIT p_batch_limit
    LOOP
        SELECT article.id INTO locked_id FROM primary_signal.articles AS article
        WHERE article.id = candidate.article_id FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        SELECT version.id INTO locked_id FROM primary_signal.content_versions AS version
        WHERE version.id = candidate.version_id AND version.article_id = candidate.article_id
        FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        UPDATE primary_signal.content_versions AS version SET extracted_text = NULL
        WHERE version.id = candidate.version_id
          AND version.article_id = candidate.article_id
          AND version.extracted_text IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM primary_signal.content_version_retention AS retention
              WHERE retention.content_version_id = version.id
                AND retention.superseded_at <= clock_timestamp() - interval '90 days'
          )
          AND EXISTS (
              SELECT 1 FROM primary_signal.articles AS article
              WHERE article.id = version.article_id
                AND article.current_content_version_id IS DISTINCT FROM version.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM primary_signal.revision_sources AS relation
              JOIN primary_signal.story_revisions AS revision
                ON revision.id = relation.revision_id
              WHERE relation.article_id = version.article_id
                AND relation.content_version_id = version.id
                AND revision.published_at IS NOT NULL
          );
        IF FOUND THEN
            RETURN NEXT candidate.version_id;
        END IF;
    END LOOP;
END
$retention_cleanup$;
""")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
    op.execute(f"DROP FUNCTION {FUNCTION}")
    # The stronger guard is safe to preserve across a downgrade.
