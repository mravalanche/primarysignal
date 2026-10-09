"""Track the latest transition out of current article status."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_16"
down_revision: str | Sequence[str] | None = "20261009_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE primary_signal.content_version_retention (
    content_version_id uuid PRIMARY KEY
        REFERENCES primary_signal.content_versions(id) ON DELETE RESTRICT,
    superseded_at timestamptz NOT NULL
);
CREATE INDEX ix_content_version_retention_superseded
    ON primary_signal.content_version_retention(superseded_at, content_version_id);
REVOKE ALL ON primary_signal.content_version_retention FROM PUBLIC;
"""
    )
    # The precise transition time is unknown for old rows. Start their clock
    # at migration time, regardless of fetched_at or any earlier observation.
    op.execute(
        """
INSERT INTO primary_signal.content_version_retention
    (content_version_id, superseded_at)
SELECT version.id, clock_timestamp()
FROM primary_signal.content_versions AS version
JOIN primary_signal.articles AS article ON article.id = version.article_id
WHERE article.current_content_version_id IS DISTINCT FROM version.id;
"""
    )
    op.execute(
        """
CREATE FUNCTION primary_signal.track_content_version_transition()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog AS $retention_transition$
BEGIN
    IF NEW.current_content_version_id IS DISTINCT FROM OLD.current_content_version_id THEN
        IF OLD.current_content_version_id IS NOT NULL THEN
            INSERT INTO primary_signal.content_version_retention
                (content_version_id, superseded_at)
            VALUES (OLD.current_content_version_id, clock_timestamp())
            ON CONFLICT (content_version_id) DO UPDATE
                SET superseded_at = EXCLUDED.superseded_at;
        END IF;
        IF NEW.current_content_version_id IS NOT NULL THEN
            DELETE FROM primary_signal.content_version_retention
            WHERE content_version_id = NEW.current_content_version_id;
        END IF;
    END IF;
    RETURN NEW;
END
$retention_transition$;
REVOKE ALL ON FUNCTION primary_signal.track_content_version_transition() FROM PUBLIC;
CREATE TRIGGER track_content_version_transition
AFTER UPDATE OF current_content_version_id ON primary_signal.articles
FOR EACH ROW EXECUTE FUNCTION primary_signal.track_content_version_transition();
"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER track_content_version_transition ON primary_signal.articles")
    op.execute("DROP FUNCTION primary_signal.track_content_version_transition()")
    op.execute("DROP TABLE primary_signal.content_version_retention")
