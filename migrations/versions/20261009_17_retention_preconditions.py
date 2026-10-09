"""Prepare immutable content history for a later scoped text cleanup capability."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_17"
down_revision: str | Sequence[str] | None = "20261009_16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PUBLISH_WITH_TEXT_LOCK = """
CREATE OR REPLACE FUNCTION primary_signal.publish_reviewed(
    p_story_id uuid, p_revision_id uuid, p_fingerprint text,
    p_expected_current uuid, p_actor text, p_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
AS $publication_publish$
DECLARE
    current_revision uuid;
    is_suppressed boolean;
    draft_status text;
    draft_number integer;
    latest_number integer;
    recorded_fingerprint text;
    source_total bigint;
    source_valid bigint;
    source_to_lock uuid;
    version_to_lock uuid;
    source_enabled boolean;
    published_time timestamptz;
BEGIN
    IF p_story_id IS NULL OR p_revision_id IS NULL
       OR p_fingerprint IS NULL OR p_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_actor IS NULL OR btrim(p_actor) = '' OR p_actor = 'system'
       OR char_length(p_actor) > 160
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 1024
    THEN
        RAISE EXCEPTION 'invalid publication decision' USING ERRCODE = '22023';
    END IF;

    SELECT current_revision_id, suppressed INTO current_revision, is_suppressed
    FROM primary_signal.stories WHERE id = p_story_id FOR UPDATE;
    IF NOT FOUND OR is_suppressed
       OR current_revision IS DISTINCT FROM p_expected_current THEN
        RAISE EXCEPTION 'story or current revision changed' USING ERRCODE = 'P0001';
    END IF;
    SELECT status, revision_number INTO draft_status, draft_number
    FROM primary_signal.story_revisions
    WHERE id = p_revision_id AND story_id = p_story_id FOR UPDATE;
    IF NOT FOUND OR draft_status <> 'draft' THEN
        RAISE EXCEPTION 'revision is not a draft' USING ERRCODE = 'P0001';
    END IF;
    SELECT max(revision_number) INTO latest_number
    FROM primary_signal.story_revisions WHERE story_id = p_story_id;
    IF latest_number <> draft_number THEN
        RAISE EXCEPTION 'a newer revision exists' USING ERRCODE = 'P0001';
    END IF;
    IF current_revision IS NOT NULL THEN
        PERFORM 1 FROM primary_signal.story_revisions
        WHERE id = current_revision AND story_id = p_story_id
          AND status = 'published' FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'current revision is not published' USING ERRCODE = 'P0001';
        END IF;
    END IF;

    SELECT input_fingerprint INTO recorded_fingerprint
    FROM primary_signal.publication_events
    WHERE revision_id = p_revision_id AND to_status = 'draft'
    ORDER BY occurred_at DESC, id DESC LIMIT 1;
    IF recorded_fingerprint IS DISTINCT FROM p_fingerprint
       OR primary_signal.draft_fingerprint_v2(p_story_id, p_revision_id)
          IS DISTINCT FROM p_fingerprint THEN
        RAISE EXCEPTION 'reviewed input fingerprint changed' USING ERRCODE = 'P0001';
    END IF;

    -- Serialize source disable and publication in source ID order.
    FOR source_to_lock IN
        SELECT DISTINCT article.source_id
        FROM primary_signal.revision_sources AS relation
        JOIN primary_signal.articles AS article ON article.id = relation.article_id
        WHERE relation.revision_id = p_revision_id
        ORDER BY article.source_id
    LOOP
        SELECT enabled INTO source_enabled FROM primary_signal.sources
        WHERE id = source_to_lock FOR SHARE;
        IF NOT FOUND OR NOT source_enabled THEN
            RAISE EXCEPTION 'source is disabled or missing' USING ERRCODE = 'P0001';
        END IF;
    END LOOP;

    -- Share locks conflict with text clearing. Lock in ID order so cleanup and
    -- concurrent publication observe one stable state without deadlocks.
    FOR version_to_lock IN
        SELECT DISTINCT relation.content_version_id
        FROM primary_signal.revision_sources AS relation
        WHERE relation.revision_id = p_revision_id
          AND relation.content_version_id IS NOT NULL
        ORDER BY relation.content_version_id
    LOOP
        PERFORM 1 FROM primary_signal.content_versions
        WHERE id = version_to_lock FOR SHARE;
    END LOOP;

    SELECT count(*), count(*) FILTER (
        WHERE version.id IS NOT NULL AND version.extracted_text IS NOT NULL
          AND attempt.id IS NOT NULL
          AND article.id IS NOT NULL AND configured.enabled
          AND EXISTS (
              SELECT 1 FROM primary_signal.article_urls AS linked
              WHERE linked.article_id = relation.article_id
                AND (linked.original_url = relation.public_url
                     OR linked.normalized_url = relation.public_url)
          )
    ) INTO source_total, source_valid
    FROM primary_signal.revision_sources AS relation
    LEFT JOIN primary_signal.content_versions AS version
      ON version.id = relation.content_version_id
     AND version.article_id = relation.article_id
    LEFT JOIN primary_signal.fetch_attempts AS attempt
      ON attempt.id = version.origin_fetch_attempt_id
     AND attempt.article_id = version.article_id
     AND attempt.status = 'fetched'
     AND attempt.resulting_content_version_id = version.id
    LEFT JOIN primary_signal.articles AS article ON article.id = relation.article_id
    LEFT JOIN primary_signal.sources AS configured ON configured.id = article.source_id
    WHERE relation.revision_id = p_revision_id;
    IF source_total = 0 OR source_valid <> source_total THEN
        RAISE EXCEPTION 'all visible sources require fetched, linked versions'
            USING ERRCODE = 'P0001';
    END IF;

    published_time := clock_timestamp();
    UPDATE primary_signal.story_revisions SET status = 'validated'
    WHERE id = p_revision_id AND status = 'draft';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'draft changed during publication' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO primary_signal.publication_events
        (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
    VALUES (uuidv7(),p_story_id,p_revision_id,'draft','validated',
            p_actor,p_reason,p_fingerprint);
    UPDATE primary_signal.story_revisions
    SET status = 'published', published_at = published_time
    WHERE id = p_revision_id AND status = 'validated';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'validated revision changed' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO primary_signal.publication_events
        (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
    VALUES (uuidv7(),p_story_id,p_revision_id,'validated','published',
            p_actor,p_reason,p_fingerprint);
    UPDATE primary_signal.stories SET current_revision_id = p_revision_id
    WHERE id = p_story_id;
    IF current_revision IS NOT NULL THEN
        UPDATE primary_signal.story_revisions SET status = 'superseded'
        WHERE id = current_revision AND status = 'published';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'current revision changed during publication'
                USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO primary_signal.publication_events
            (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
        VALUES (uuidv7(),p_story_id,current_revision,'published','superseded',
                p_actor,p_reason,NULL);
    END IF;
END
$publication_publish$;
"""

PUBLISH_BEFORE_TEXT_LOCK = """
CREATE OR REPLACE FUNCTION primary_signal.publish_reviewed(
    p_story_id uuid, p_revision_id uuid, p_fingerprint text,
    p_expected_current uuid, p_actor text, p_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
AS $publication_publish$
DECLARE
    current_revision uuid;
    is_suppressed boolean;
    draft_status text;
    draft_number integer;
    latest_number integer;
    recorded_fingerprint text;
    source_total bigint;
    source_valid bigint;
    source_to_lock uuid;
    source_enabled boolean;
    published_time timestamptz;
BEGIN
    IF p_story_id IS NULL OR p_revision_id IS NULL
       OR p_fingerprint IS NULL OR p_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_actor IS NULL OR btrim(p_actor) = '' OR p_actor = 'system'
       OR char_length(p_actor) > 160
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 1024
    THEN
        RAISE EXCEPTION 'invalid publication decision' USING ERRCODE = '22023';
    END IF;

    SELECT current_revision_id, suppressed INTO current_revision, is_suppressed
    FROM primary_signal.stories WHERE id = p_story_id FOR UPDATE;
    IF NOT FOUND OR is_suppressed
       OR current_revision IS DISTINCT FROM p_expected_current THEN
        RAISE EXCEPTION 'story or current revision changed' USING ERRCODE = 'P0001';
    END IF;
    SELECT status, revision_number INTO draft_status, draft_number
    FROM primary_signal.story_revisions
    WHERE id = p_revision_id AND story_id = p_story_id FOR UPDATE;
    IF NOT FOUND OR draft_status <> 'draft' THEN
        RAISE EXCEPTION 'revision is not a draft' USING ERRCODE = 'P0001';
    END IF;
    SELECT max(revision_number) INTO latest_number
    FROM primary_signal.story_revisions WHERE story_id = p_story_id;
    IF latest_number <> draft_number THEN
        RAISE EXCEPTION 'a newer revision exists' USING ERRCODE = 'P0001';
    END IF;
    IF current_revision IS NOT NULL THEN
        PERFORM 1 FROM primary_signal.story_revisions
        WHERE id = current_revision AND story_id = p_story_id
          AND status = 'published' FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'current revision is not published' USING ERRCODE = 'P0001';
        END IF;
    END IF;

    SELECT input_fingerprint INTO recorded_fingerprint
    FROM primary_signal.publication_events
    WHERE revision_id = p_revision_id AND to_status = 'draft'
    ORDER BY occurred_at DESC, id DESC LIMIT 1;
    IF recorded_fingerprint IS DISTINCT FROM p_fingerprint
       OR primary_signal.draft_fingerprint_v2(p_story_id, p_revision_id)
          IS DISTINCT FROM p_fingerprint THEN
        RAISE EXCEPTION 'reviewed input fingerprint changed' USING ERRCODE = 'P0001';
    END IF;

    -- Serialize source disable and publication in source ID order.
    FOR source_to_lock IN
        SELECT DISTINCT article.source_id
        FROM primary_signal.revision_sources AS relation
        JOIN primary_signal.articles AS article ON article.id = relation.article_id
        WHERE relation.revision_id = p_revision_id
        ORDER BY article.source_id
    LOOP
        SELECT enabled INTO source_enabled FROM primary_signal.sources
        WHERE id = source_to_lock FOR SHARE;
        IF NOT FOUND OR NOT source_enabled THEN
            RAISE EXCEPTION 'source is disabled or missing' USING ERRCODE = 'P0001';
        END IF;
    END LOOP;

    SELECT count(*), count(*) FILTER (
        WHERE version.id IS NOT NULL AND attempt.id IS NOT NULL
          AND article.id IS NOT NULL AND configured.enabled
          AND EXISTS (
              SELECT 1 FROM primary_signal.article_urls AS linked
              WHERE linked.article_id = relation.article_id
                AND (linked.original_url = relation.public_url
                     OR linked.normalized_url = relation.public_url)
          )
    ) INTO source_total, source_valid
    FROM primary_signal.revision_sources AS relation
    LEFT JOIN primary_signal.content_versions AS version
      ON version.id = relation.content_version_id
     AND version.article_id = relation.article_id
    LEFT JOIN primary_signal.fetch_attempts AS attempt
      ON attempt.id = version.origin_fetch_attempt_id
     AND attempt.article_id = version.article_id
     AND attempt.status = 'fetched'
     AND attempt.resulting_content_version_id = version.id
    LEFT JOIN primary_signal.articles AS article ON article.id = relation.article_id
    LEFT JOIN primary_signal.sources AS configured ON configured.id = article.source_id
    WHERE relation.revision_id = p_revision_id;
    IF source_total = 0 OR source_valid <> source_total THEN
        RAISE EXCEPTION 'all visible sources require fetched, linked versions'
            USING ERRCODE = 'P0001';
    END IF;

    published_time := clock_timestamp();
    UPDATE primary_signal.story_revisions SET status = 'validated'
    WHERE id = p_revision_id AND status = 'draft';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'draft changed during publication' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO primary_signal.publication_events
        (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
    VALUES (uuidv7(),p_story_id,p_revision_id,'draft','validated',
            p_actor,p_reason,p_fingerprint);
    UPDATE primary_signal.story_revisions
    SET status = 'published', published_at = published_time
    WHERE id = p_revision_id AND status = 'validated';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'validated revision changed' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO primary_signal.publication_events
        (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
    VALUES (uuidv7(),p_story_id,p_revision_id,'validated','published',
            p_actor,p_reason,p_fingerprint);
    UPDATE primary_signal.stories SET current_revision_id = p_revision_id
    WHERE id = p_story_id;
    IF current_revision IS NOT NULL THEN
        UPDATE primary_signal.story_revisions SET status = 'superseded'
        WHERE id = current_revision AND status = 'published';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'current revision changed during publication'
                USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO primary_signal.publication_events
            (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
        VALUES (uuidv7(),p_story_id,current_revision,'published','superseded',
                p_actor,p_reason,NULL);
    END IF;
END
$publication_publish$;
"""


def upgrade() -> None:
    # Only the migration owner can currently reach this narrow exception to
    # immutable history. No runtime role receives UPDATE on content_versions.
    op.execute(
        "ALTER TABLE primary_signal.content_versions ALTER COLUMN extracted_text DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE primary_signal.content_versions DROP CONSTRAINT "
        "uq_content_versions_article_id_normalization_version_no_1bda"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_content_versions_text_bearing_hash ON "
        "primary_signal.content_versions(article_id,normalization_version,normalized_content_hash) "
        "WHERE extracted_text IS NOT NULL"
    )
    op.execute("""
CREATE FUNCTION primary_signal.find_text_bearing_content_version(
    p_article_id uuid, p_normalization_version integer, p_hash text
) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
    SELECT id FROM primary_signal.content_versions
    WHERE article_id = p_article_id
      AND normalization_version = p_normalization_version
      AND normalized_content_hash = p_hash
      AND extracted_text IS NOT NULL
$$;
""")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "primary_signal.find_text_bearing_content_version(uuid,integer,text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "primary_signal.find_text_bearing_content_version(uuid,integer,text) "
        "TO primary_signal_cap_article_persist"
    )
    op.execute("DROP TRIGGER protect_content_versions ON primary_signal.content_versions")
    op.execute("""
CREATE FUNCTION primary_signal.guard_content_version_text_clear()
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
    -- A fetch updates the article pointer under an article row lock. Wait for
    -- that transition before deciding whether this version is eligible.
    PERFORM 1 FROM primary_signal.articles
    WHERE id = OLD.article_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM primary_signal.articles
        WHERE current_content_version_id = OLD.id
    ) OR EXISTS (
        SELECT 1 FROM primary_signal.revision_sources AS relation
        JOIN primary_signal.publication_events AS event
          ON event.revision_id = relation.revision_id
        WHERE relation.content_version_id = OLD.id
          AND event.to_status = 'published'
    ) THEN
        RAISE EXCEPTION 'current or published content text must be retained'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
""")
    op.execute(
        "REVOKE ALL ON FUNCTION primary_signal.guard_content_version_text_clear() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER protect_content_versions BEFORE UPDATE OR DELETE "
        "ON primary_signal.content_versions FOR EACH ROW EXECUTE FUNCTION "
        "primary_signal.guard_content_version_text_clear()"
    )
    op.execute(PUBLISH_WITH_TEXT_LOCK)


def downgrade() -> None:
    op.execute(PUBLISH_BEFORE_TEXT_LOCK)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "primary_signal.find_text_bearing_content_version(uuid,integer,text) "
        "FROM primary_signal_cap_article_persist"
    )
    op.execute("DROP FUNCTION primary_signal.find_text_bearing_content_version(uuid,integer,text)")
    op.execute("DROP TRIGGER protect_content_versions ON primary_signal.content_versions")
    op.execute("DROP FUNCTION primary_signal.guard_content_version_text_clear()")
    op.execute(
        "CREATE TRIGGER protect_content_versions BEFORE UPDATE OR DELETE "
        "ON primary_signal.content_versions FOR EACH ROW EXECUTE FUNCTION "
        "primary_signal.reject_immutable_history_change()"
    )
    op.execute("DROP INDEX primary_signal.uq_content_versions_text_bearing_hash")
    op.execute(
        "ALTER TABLE primary_signal.content_versions ADD CONSTRAINT "
        "uq_content_versions_article_id_normalization_version_no_1bda "
        "UNIQUE (article_id,normalization_version,normalized_content_hash)"
    )
    # A downgrade with cleared text is intentionally rejected by PostgreSQL.
    op.execute(
        "ALTER TABLE primary_signal.content_versions ALTER COLUMN extracted_text SET NOT NULL"
    )
