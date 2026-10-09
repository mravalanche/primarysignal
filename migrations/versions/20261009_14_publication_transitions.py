"""Require audited publication transitions through restricted functions."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_14"
down_revision: str | Sequence[str] | None = "20261009_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_publication_write"
FINGERPRINT = "primary_signal.draft_fingerprint_v2(uuid,uuid)"
LOCK_DRAFT = "primary_signal.lock_story_for_draft(text)"
FINALIZE = "primary_signal.finalize_draft(uuid,uuid)"
PUBLISH = "primary_signal.publish_reviewed(uuid,uuid,text,uuid,text,text)"
SUPPRESS = "primary_signal.suppress_reviewed(uuid,uuid,text,text)"


def upgrade() -> None:
    # Revoke the column grants from 09; retain draft INSERT grants.
    op.execute(
        f"REVOKE UPDATE (current_revision_id,suppressed) ON primary_signal.stories FROM {ROLE}"
    )
    op.execute(f"REVOKE UPDATE (status,published_at) ON primary_signal.story_revisions FROM {ROLE}")
    op.execute(
        f"REVOKE INSERT (id,story_id,revision_id,from_status,to_status,actor,reason,"
        f"input_fingerprint) ON primary_signal.publication_events FROM {ROLE}"
    )

    op.execute(
        """
CREATE FUNCTION primary_signal.draft_fingerprint_v2(
    p_story_id uuid, p_revision_id uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
AS $publication_fingerprint$
DECLARE
    payload jsonb;
BEGIN
    SELECT jsonb_build_object(
        'story_id', story.id,
        'revision_id', revision.id,
        'revision_number', revision.revision_number,
        'slug', story.slug,
        'headline', revision.headline,
        'synthesis', revision.synthesis,
        'why_it_matters', revision.why_it_matters,
        'primary_topic', revision.primary_topic,
        'story_type', revision.story_type,
        'uk_relevant', revision.uk_relevant,
        'first_reported_at', revision.first_reported_at,
        'latest_material_update_at', revision.latest_material_update_at,
        'sources', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', source.source_id,
                'position', source.position,
                'title', source.title,
                'publisher', source.publisher,
                'url', source.public_url,
                'first_published_at', source.first_published_at,
                'is_primary', source.is_primary,
                'article_id', source.article_id,
                'content_version_id', source.content_version_id
            ) ORDER BY source.position)
            FROM primary_signal.revision_sources AS source
            WHERE source.revision_id = revision.id
        ), '[]'::jsonb),
        'tags', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', relation.tag_id, 'position', relation.position
            ) ORDER BY relation.position)
            FROM primary_signal.revision_tags AS relation
            WHERE relation.revision_id = revision.id
        ), '[]'::jsonb),
        'signals', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'kind', signal.kind,
                'evidence', COALESCE((
                    SELECT jsonb_agg(evidence.source_id ORDER BY evidence.source_id)
                    FROM primary_signal.signal_evidence AS evidence
                    WHERE evidence.revision_id = signal.revision_id
                      AND evidence.kind = signal.kind
                ), '[]'::jsonb)) ORDER BY signal.kind)
            FROM primary_signal.revision_signals AS signal
            WHERE signal.revision_id = revision.id
        ), '[]'::jsonb)
    ) INTO payload
    FROM primary_signal.stories AS story
    JOIN primary_signal.story_revisions AS revision
      ON revision.story_id = story.id
    WHERE story.id = p_story_id AND revision.id = p_revision_id;

    IF payload IS NULL THEN
        RAISE EXCEPTION 'draft is missing' USING ERRCODE = 'P0001';
    END IF;
    RETURN encode(sha256(convert_to(
        'primary_signal/draft/v2:' || payload::text, 'UTF8'
    )), 'hex');
END
$publication_fingerprint$;
"""
    )

    # Child writes and finalization serialize on the revision row.
    op.execute(
        """
CREATE OR REPLACE FUNCTION primary_signal.guard_revision_child()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog AS $$
DECLARE current_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.revision_id <> NEW.revision_id THEN
        RAISE EXCEPTION 'revision membership cannot move';
    END IF;
    SELECT status INTO current_status FROM primary_signal.story_revisions
        WHERE id = COALESCE(NEW.revision_id, OLD.revision_id) FOR UPDATE;
    IF current_status <> 'draft' OR EXISTS (
        SELECT 1 FROM primary_signal.publication_events
        WHERE revision_id = COALESCE(NEW.revision_id, OLD.revision_id)
          AND to_status = 'draft'
    ) THEN
        RAISE EXCEPTION 'finalized revision contents are immutable';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
"""
    )

    op.execute(
        """
CREATE FUNCTION primary_signal.lock_story_for_draft(p_slug text)
RETURNS TABLE(id uuid, suppressed boolean)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET statement_timeout = '5s'
AS $publication_lock_draft$
BEGIN
    IF p_slug IS NULL OR char_length(p_slug) > 160
       OR p_slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$' THEN
        RAISE EXCEPTION 'invalid story slug' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT story.id, story.suppressed FROM primary_signal.stories AS story
    WHERE story.slug = p_slug FOR UPDATE OF story;
END
$publication_lock_draft$;
"""
    )

    op.execute(
        """
CREATE FUNCTION primary_signal.finalize_draft(
    p_story_id uuid, p_revision_id uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET TimeZone = 'UTC'
AS $publication_finalize$
DECLARE
    draft_status text;
    draft_number integer;
    latest_number integer;
    fingerprint text;
BEGIN
    IF p_story_id IS NULL OR p_revision_id IS NULL THEN
        RAISE EXCEPTION 'story and revision are required' USING ERRCODE = '22023';
    END IF;
    PERFORM 1 FROM primary_signal.stories
    WHERE id = p_story_id AND NOT suppressed FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'story is missing or suppressed' USING ERRCODE = 'P0001';
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
    IF NOT EXISTS (
        SELECT 1 FROM primary_signal.revision_sources WHERE revision_id = p_revision_id
    ) OR EXISTS (
        SELECT 1 FROM primary_signal.publication_events
        WHERE revision_id = p_revision_id AND to_status = 'draft'
    ) THEN
        RAISE EXCEPTION 'draft has no sources or was already finalized'
            USING ERRCODE = 'P0001';
    END IF;
    fingerprint := primary_signal.draft_fingerprint_v2(p_story_id, p_revision_id);
    INSERT INTO primary_signal.publication_events
        (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
    VALUES (uuidv7(),p_story_id,p_revision_id,NULL,'draft',
            'system','draft created',fingerprint);
    RETURN fingerprint;
END
$publication_finalize$;
"""
    )

    op.execute(
        """
CREATE FUNCTION primary_signal.publish_reviewed(
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
    )

    op.execute(
        """
CREATE FUNCTION primary_signal.suppress_reviewed(
    p_story_id uuid, p_expected_current uuid, p_actor text, p_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
AS $publication_suppress$
DECLARE
    current_revision uuid;
    is_suppressed boolean;
BEGIN
    IF p_story_id IS NULL OR p_expected_current IS NULL
       OR p_actor IS NULL OR btrim(p_actor) = '' OR p_actor = 'system'
       OR char_length(p_actor) > 160
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 1024
    THEN
        RAISE EXCEPTION 'invalid suppression decision' USING ERRCODE = '22023';
    END IF;
    SELECT current_revision_id, suppressed INTO current_revision, is_suppressed
    FROM primary_signal.stories WHERE id = p_story_id FOR UPDATE;
    IF NOT FOUND OR is_suppressed
       OR current_revision IS DISTINCT FROM p_expected_current THEN
        RAISE EXCEPTION 'story or current revision changed' USING ERRCODE = 'P0001';
    END IF;
    PERFORM 1 FROM primary_signal.story_revisions
    WHERE id = current_revision AND story_id = p_story_id
      AND status = 'published' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'current revision is not published' USING ERRCODE = 'P0001';
    END IF;
    UPDATE primary_signal.stories SET suppressed = true WHERE id = p_story_id;
    UPDATE primary_signal.story_revisions SET status = 'suppressed'
    WHERE id = current_revision AND status = 'published';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'current revision changed during suppression'
            USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO primary_signal.publication_events
        (id,story_id,revision_id,from_status,to_status,actor,reason,input_fingerprint)
    VALUES (uuidv7(),p_story_id,current_revision,'published','suppressed',
            p_actor,p_reason,NULL);
END
$publication_suppress$;
"""
    )

    for function in (FINGERPRINT, LOCK_DRAFT, FINALIZE, PUBLISH, SUPPRESS):
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
    for function in (LOCK_DRAFT, FINALIZE, PUBLISH, SUPPRESS):
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {ROLE}")


def downgrade() -> None:
    for function in (LOCK_DRAFT, FINALIZE, PUBLISH, SUPPRESS):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM {ROLE}")
    for function in (SUPPRESS, PUBLISH, FINALIZE, LOCK_DRAFT, FINGERPRINT):
        op.execute(f"DROP FUNCTION {function}")

    op.execute(
        """
CREATE OR REPLACE FUNCTION primary_signal.guard_revision_child()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE current_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.revision_id <> NEW.revision_id THEN
        RAISE EXCEPTION 'revision membership cannot move';
    END IF;
    SELECT status INTO current_status FROM primary_signal.story_revisions
        WHERE id = COALESCE(NEW.revision_id, OLD.revision_id) FOR UPDATE;
    IF current_status <> 'draft' THEN
        RAISE EXCEPTION 'validated revision contents are immutable';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
"""
    )
    op.execute("ALTER FUNCTION primary_signal.guard_revision_child() SECURITY INVOKER")

    op.execute(f"GRANT UPDATE (current_revision_id,suppressed) ON primary_signal.stories TO {ROLE}")
    op.execute(f"GRANT UPDATE (status,published_at) ON primary_signal.story_revisions TO {ROLE}")
    op.execute(
        f"GRANT INSERT (id,story_id,revision_id,from_status,to_status,actor,reason,"
        f"input_fingerprint) ON primary_signal.publication_events TO {ROLE}"
    )
