"""Audit source state transitions through an execute-only capability."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261028_20"
down_revision: str | Sequence[str] | None = "20261009_19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_source_control"
FUNCTION = "primary_signal.set_source_enabled(text,boolean,text)"


def upgrade() -> None:
    op.create_table(
        "source_state_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("old_enabled", sa.Boolean(), nullable=False),
        sa.Column("new_enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "old_enabled <> new_enabled", name=op.f("ck_source_state_events_changed_state")
        ),
        sa.CheckConstraint(
            r"char_length(reason) BETWEEN 1 AND 500 AND btrim(reason, U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000') = reason AND reason !~ U&'[\0001-\001F\007F-\009F]'",
            name=op.f("ck_source_state_events_valid_reason"),
        ),
        sa.CheckConstraint(
            "char_length(actor) > 0", name=op.f("ck_source_state_events_valid_actor")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["primary_signal.sources.id"],
            ondelete="RESTRICT",
            name=op.f("fk_source_state_events_source_id_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_state_events")),
        schema="primary_signal",
    )
    op.create_index(
        "ix_source_state_events_source_time",
        "source_state_events",
        ["source_id", "occurred_at", "id"],
        schema="primary_signal",
    )
    op.execute("""
CREATE TRIGGER protect_source_state_events
BEFORE UPDATE OR DELETE ON primary_signal.source_state_events
FOR EACH ROW EXECUTE FUNCTION primary_signal.reject_immutable_history_change();
""")
    op.execute(r"""
CREATE FUNCTION primary_signal.set_source_enabled(
    p_source_key text, p_enabled boolean, p_reason text
) RETURNS TABLE (
    source_key text, enabled boolean, changed boolean,
    updated_at timestamptz, event_id uuid
) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
SET statement_timeout = '5s'
AS $source_control$
DECLARE
    selected_source record;
    change_time timestamptz;
    new_event_id uuid;
    clean_reason text;
BEGIN
    IF p_source_key IS NULL OR char_length(p_source_key) NOT BETWEEN 1 AND 100
       OR p_source_key !~ '^[a-z0-9]+(-[a-z0-9]+)*$'
       OR p_source_key ~ '[^a-z0-9-]'
       OR p_enabled IS NULL
       OR p_reason IS NULL OR p_reason ~ U&'[\0001-\001F\007F-\009F]'
    THEN
        RAISE EXCEPTION 'invalid source state transition' USING ERRCODE = '22023';
    END IF;
    clean_reason := btrim(p_reason, U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000');
    IF char_length(clean_reason) NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'invalid source state reason' USING ERRCODE = '22023';
    END IF;

    -- This function never locks feeds or jobs. Ingestion and scheduling hold
    -- the existing shared source lock through their own short transactions.
    SELECT source.id, source.enabled, source.updated_at INTO selected_source
    FROM primary_signal.sources AS source
    WHERE source.source_key = p_source_key FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source is missing' USING ERRCODE = 'P0002';
    END IF;
    change_time := clock_timestamp();
    IF selected_source.enabled = p_enabled THEN
        RETURN QUERY SELECT p_source_key, selected_source.enabled, false,
                            selected_source.updated_at, NULL::uuid;
        RETURN;
    END IF;

    new_event_id := uuidv7();
    UPDATE primary_signal.sources AS source
    SET enabled = p_enabled, updated_at = change_time
    WHERE source.id = selected_source.id;
    INSERT INTO primary_signal.source_state_events
        (id,source_id,old_enabled,new_enabled,reason,actor,occurred_at)
    VALUES (new_event_id,selected_source.id,selected_source.enabled,p_enabled,
            clean_reason,session_user,change_time);
    RETURN QUERY SELECT p_source_key, p_enabled, true, change_time, new_event_id;
END
$source_control$;
""")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ROLE}")
    op.execute(
        "GRANT EXECUTE ON FUNCTION primary_signal.lock_source_enabled(uuid) TO primary_signal_cap_feed_schedule"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE EXECUTE ON FUNCTION primary_signal.lock_source_enabled(uuid) FROM primary_signal_cap_feed_schedule"
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
    op.execute(f"DROP FUNCTION {FUNCTION}")
    op.drop_index(
        "ix_source_state_events_source_time",
        table_name="source_state_events",
        schema="primary_signal",
    )
    op.drop_table("source_state_events", schema="primary_signal")
