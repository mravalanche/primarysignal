"""Permit ingestion roles to lock source state without source write grants."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_11"
down_revision: str | Sequence[str] | None = "20261009_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "primary_signal.lock_source_enabled(uuid)"
ROLES = ("primary_signal_cap_article_persist", "primary_signal_cap_feed_poll")


def upgrade() -> None:
    """Expose one boolean source-state check that holds a shared row lock."""

    op.execute(
        """
CREATE FUNCTION primary_signal.lock_source_enabled(p_source_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET statement_timeout = '5s'
AS $source_read_lock$
DECLARE
    source_enabled boolean;
BEGIN
    SELECT enabled INTO source_enabled
    FROM primary_signal.sources
    WHERE id = p_source_id
    FOR SHARE;
    RETURN coalesce(source_enabled, false);
END
$source_read_lock$;
"""
    )
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    for role in ROLES:
        op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {role}")


def downgrade() -> None:
    for role in ROLES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM {role}")
    op.execute(f"DROP FUNCTION {FUNCTION}")
