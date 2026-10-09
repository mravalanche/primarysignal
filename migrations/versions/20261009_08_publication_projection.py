"""Create immutable publication snapshots and a restricted current projection."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_08"
down_revision: str | Sequence[str] | None = "20261009_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep private provenance in base tables and expose current snapshots only."""

    op.execute(
        """
CREATE TABLE primary_signal.stories (
    id uuid PRIMARY KEY,
    slug text NOT NULL CONSTRAINT uq_stories_slug UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
        AND char_length(slug) <= 160),
    current_revision_id uuid,
    suppressed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_stories_id_current UNIQUE (id, current_revision_id)
);
CREATE TABLE primary_signal.story_revisions (
    id uuid PRIMARY KEY,
    story_id uuid NOT NULL REFERENCES primary_signal.stories(id) ON DELETE RESTRICT,
    revision_number integer NOT NULL CHECK (revision_number > 0),
    status text NOT NULL DEFAULT 'draft' CHECK
        (status IN ('draft','validated','published','suppressed','superseded')),
    headline text NOT NULL CHECK (char_length(headline) BETWEEN 1 AND 300),
    synthesis text NOT NULL CHECK (char_length(synthesis) > 0),
    why_it_matters text NOT NULL CHECK (char_length(why_it_matters) > 0),
    primary_topic text NOT NULL CHECK (primary_topic IN (
        'vulnerabilities-and-exploitation','threat-activity-and-incidents',
        'security-engineering','policy-and-strategy','research-and-tools')),
    story_type text NOT NULL CHECK (story_type IN (
        'news','research','advisory','incident','analysis','opinion','tool-release')),
    uk_relevant boolean NOT NULL DEFAULT false,
    first_reported_at timestamptz NOT NULL,
    latest_material_update_at timestamptz NOT NULL,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (latest_material_update_at >= first_reported_at),
    CHECK ((status IN ('published','superseded','suppressed')) = (published_at IS NOT NULL)),
    CONSTRAINT uq_story_revisions_story_id_id UNIQUE (story_id, id),
    CONSTRAINT uq_story_revisions_story_id_revision_number UNIQUE (story_id, revision_number)
);
ALTER TABLE primary_signal.stories
    ADD CONSTRAINT fk_stories_current_same_story
    FOREIGN KEY (id, current_revision_id)
    REFERENCES primary_signal.story_revisions(story_id, id) ON DELETE RESTRICT;
CREATE INDEX ix_story_revisions_story_status
    ON primary_signal.story_revisions(story_id, status);
CREATE INDEX ix_stories_current ON primary_signal.stories(current_revision_id)
    WHERE current_revision_id IS NOT NULL AND NOT suppressed;

CREATE TABLE primary_signal.revision_sources (
    revision_id uuid NOT NULL REFERENCES primary_signal.story_revisions(id) ON DELETE RESTRICT,
    source_id text NOT NULL CHECK (source_id ~ '^[a-z0-9]+([._-][a-z0-9]+)*$'
        AND char_length(source_id) <= 160),
    position integer NOT NULL CHECK (position > 0),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    publisher text NOT NULL CHECK (char_length(publisher) BETWEEN 1 AND 160),
    public_url text NOT NULL CHECK (public_url ~ '^https?://'
        AND char_length(public_url) <= 2048),
    first_published_at timestamptz,
    is_primary boolean NOT NULL DEFAULT false,
    article_id uuid,
    content_version_id uuid,
    PRIMARY KEY (revision_id, source_id),
    CONSTRAINT uq_revision_sources_revision_id_position UNIQUE (revision_id, position),
    CHECK ((article_id IS NULL) = (content_version_id IS NULL)),
    FOREIGN KEY (article_id, content_version_id)
        REFERENCES primary_signal.content_versions(article_id, id) ON DELETE RESTRICT
);
CREATE TABLE primary_signal.tags (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9]+([._-][a-z0-9]+)*$'
        AND char_length(id) <= 160),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 120),
    kind text NOT NULL CHECK (kind IN (
        'cve','organisation','product','actor','technology','sector','curated'))
);
CREATE TABLE primary_signal.revision_tags (
    revision_id uuid NOT NULL REFERENCES primary_signal.story_revisions(id) ON DELETE RESTRICT,
    tag_id text NOT NULL REFERENCES primary_signal.tags(id) ON DELETE RESTRICT,
    position integer NOT NULL CHECK (position > 0),
    PRIMARY KEY (revision_id, tag_id),
    CONSTRAINT uq_revision_tags_revision_id_position UNIQUE (revision_id, position)
);
CREATE TABLE primary_signal.revision_signals (
    revision_id uuid NOT NULL REFERENCES primary_signal.story_revisions(id) ON DELETE RESTRICT,
    kind text NOT NULL CHECK (kind IN (
        'primary-source','official-advisory','active-exploitation',
        'exploit-available','actionable','confirmed-incident','developing',
        'widely-reported','deep-read')),
    PRIMARY KEY (revision_id, kind)
);
CREATE TABLE primary_signal.signal_evidence (
    revision_id uuid NOT NULL,
    kind text NOT NULL,
    source_id text NOT NULL,
    PRIMARY KEY (revision_id, kind, source_id),
    FOREIGN KEY (revision_id, kind)
        REFERENCES primary_signal.revision_signals(revision_id, kind) ON DELETE RESTRICT,
    FOREIGN KEY (revision_id, source_id)
        REFERENCES primary_signal.revision_sources(revision_id, source_id) ON DELETE RESTRICT
);

CREATE FUNCTION primary_signal.guard_story_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.published_at IS NOT NULL THEN
            RAISE EXCEPTION 'new story revisions must begin as drafts';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'story revisions are retained';
    END IF;
    IF OLD.status = 'validated' AND NEW.status = 'published' AND NOT EXISTS (
        SELECT 1 FROM primary_signal.revision_sources
        WHERE revision_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'published story revision requires a source';
    END IF;
    IF OLD.status = 'validated' AND NEW.status = 'published' AND EXISTS (
        SELECT 1 FROM primary_signal.revision_signals AS signal
        WHERE signal.revision_id = NEW.id
          AND NOT EXISTS (
              SELECT 1 FROM primary_signal.signal_evidence AS evidence
              WHERE evidence.revision_id = signal.revision_id
                AND evidence.kind = signal.kind
          )
    ) THEN
        RAISE EXCEPTION 'published signal requires source evidence';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','published_at']::text[])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status','published_at']::text[])
       OR NOT (
           (OLD.status = 'draft' AND NEW.status = 'validated' AND NEW.published_at IS NULL)
           OR (OLD.status = 'validated' AND NEW.status = 'published'
               AND NEW.published_at IS NOT NULL)
           OR (OLD.status = 'published' AND NEW.status IN ('suppressed','superseded')
               AND NEW.published_at = OLD.published_at)
       )
    THEN
        RAISE EXCEPTION 'invalid story revision transition';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_story_revision BEFORE INSERT OR UPDATE OR DELETE
ON primary_signal.story_revisions FOR EACH ROW
EXECUTE FUNCTION primary_signal.guard_story_revision();

CREATE TRIGGER guard_tag_identity BEFORE UPDATE OR DELETE
ON primary_signal.tags FOR EACH ROW
EXECUTE FUNCTION primary_signal.reject_immutable_history_change();

CREATE FUNCTION primary_signal.guard_revision_child() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
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
CREATE TRIGGER guard_revision_sources BEFORE INSERT OR UPDATE OR DELETE
ON primary_signal.revision_sources FOR EACH ROW
EXECUTE FUNCTION primary_signal.guard_revision_child();
CREATE TRIGGER guard_revision_tags BEFORE INSERT OR UPDATE OR DELETE
ON primary_signal.revision_tags FOR EACH ROW
EXECUTE FUNCTION primary_signal.guard_revision_child();
CREATE TRIGGER guard_revision_signals BEFORE INSERT OR UPDATE OR DELETE
ON primary_signal.revision_signals FOR EACH ROW
EXECUTE FUNCTION primary_signal.guard_revision_child();
CREATE TRIGGER guard_signal_evidence BEFORE INSERT OR UPDATE OR DELETE
ON primary_signal.signal_evidence FOR EACH ROW
EXECUTE FUNCTION primary_signal.guard_revision_child();

CREATE FUNCTION primary_signal.guard_current_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.current_revision_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM primary_signal.story_revisions AS revision
           WHERE revision.id = NEW.current_revision_id
             AND revision.story_id = NEW.id
             AND revision.status = 'published'
       )
    THEN
        RAISE EXCEPTION 'current revision must be published for this story';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_current_revision BEFORE INSERT OR UPDATE OF current_revision_id
ON primary_signal.stories FOR EACH ROW
EXECUTE FUNCTION primary_signal.guard_current_revision();
"""
    )

    # The view owner receives only the columns the views read. It never owns
    # private base tables; the public reader has no access to this schema.
    owner = "primary_signal_public_owner"
    reader = "primary_signal_cap_public_read"
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {owner}")
    for table, columns in {
        "stories": "id, slug, current_revision_id, suppressed",
        "story_revisions": (
            "id, status, headline, synthesis, why_it_matters, primary_topic, story_type, "
            "uk_relevant, first_reported_at, latest_material_update_at"
        ),
        "revision_sources": (
            "revision_id, source_id, position, title, publisher, public_url, "
            "first_published_at, is_primary"
        ),
        "tags": "id, label, kind",
        "revision_tags": "revision_id, tag_id, position",
        "revision_signals": "revision_id, kind",
        "signal_evidence": "revision_id, kind, source_id",
    }.items():
        op.execute(f"GRANT SELECT ({columns}) ON primary_signal.{table} TO {owner}")

    op.execute("CREATE SCHEMA primary_signal_public")
    op.execute("REVOKE ALL ON SCHEMA primary_signal_public FROM PUBLIC")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA primary_signal_public TO {owner}")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA primary_signal_public FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA primary_signal_public FROM PUBLIC")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal_public REVOKE ALL ON TABLES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal_public "
        "REVOKE ALL ON FUNCTIONS FROM PUBLIC"
    )
    op.execute(
        """
CREATE VIEW primary_signal_public.stories WITH (security_barrier=true) AS
SELECT s.id AS story_id, s.slug, r.id AS revision_id, r.headline,
       r.synthesis, r.why_it_matters, r.primary_topic, r.story_type,
       r.uk_relevant, r.first_reported_at, r.latest_material_update_at,
       (SELECT count(*)::integer FROM primary_signal.revision_sources AS source
        WHERE source.revision_id = r.id) AS source_count
FROM primary_signal.stories AS s
JOIN primary_signal.story_revisions AS r ON r.id = s.current_revision_id
WHERE NOT s.suppressed AND r.status = 'published';

CREATE VIEW primary_signal_public.sources WITH (security_barrier=true) AS
SELECT story.story_id, story.slug, story.revision_id, source.source_id,
       source.position, source.title, source.publisher, source.public_url,
       source.first_published_at, source.is_primary
FROM primary_signal_public.stories AS story
JOIN primary_signal.revision_sources AS source
  ON source.revision_id = story.revision_id;

CREATE VIEW primary_signal_public.story_tags WITH (security_barrier=true) AS
SELECT story.story_id, story.slug, story.revision_id, relation.tag_id, relation.position
FROM primary_signal_public.stories AS story
JOIN primary_signal.revision_tags AS relation
  ON relation.revision_id = story.revision_id;

CREATE VIEW primary_signal_public.tags WITH (security_barrier=true) AS
SELECT tag.id, tag.label, tag.kind
FROM primary_signal.tags AS tag
WHERE EXISTS (
    SELECT 1 FROM primary_signal_public.story_tags AS relation
    WHERE relation.tag_id = tag.id
);

CREATE VIEW primary_signal_public.signals WITH (security_barrier=true) AS
SELECT story.story_id, story.slug, story.revision_id, signal.kind
FROM primary_signal_public.stories AS story
JOIN primary_signal.revision_signals AS signal
  ON signal.revision_id = story.revision_id
WHERE EXISTS (
    SELECT 1 FROM primary_signal.signal_evidence AS evidence
    WHERE evidence.revision_id = signal.revision_id AND evidence.kind = signal.kind
);

CREATE VIEW primary_signal_public.signal_evidence WITH (security_barrier=true) AS
SELECT signal.story_id, signal.slug, signal.revision_id,
       signal.kind, evidence.source_id
FROM primary_signal_public.signals AS signal
JOIN primary_signal.signal_evidence AS evidence
  ON evidence.revision_id = signal.revision_id AND evidence.kind = signal.kind;
"""
    )
    for view in ("stories", "sources", "story_tags", "tags", "signals", "signal_evidence"):
        op.execute(f"ALTER VIEW primary_signal_public.{view} OWNER TO {owner}")
        op.execute(f"GRANT SELECT ON primary_signal_public.{view} TO {reader}")
    op.execute(f"ALTER SCHEMA primary_signal_public OWNER TO {owner}")
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal_public TO {reader}")


def downgrade() -> None:
    """Remove the publication foundation without touching ingestion history."""

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal_public REVOKE ALL ON TABLES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal_public "
        "REVOKE ALL ON FUNCTIONS FROM PUBLIC"
    )
    op.execute("DROP SCHEMA primary_signal_public CASCADE")
    op.execute("DROP TABLE primary_signal.signal_evidence")
    op.execute("DROP TABLE primary_signal.revision_signals")
    op.execute("DROP TABLE primary_signal.revision_tags")
    op.execute("DROP TABLE primary_signal.tags")
    op.execute("DROP TABLE primary_signal.revision_sources")
    op.execute("ALTER TABLE primary_signal.stories DROP CONSTRAINT fk_stories_current_same_story")
    op.execute("DROP TABLE primary_signal.story_revisions")
    op.execute("DROP TABLE primary_signal.stories")
    op.execute("DROP FUNCTION primary_signal.guard_current_revision()")
    op.execute("DROP FUNCTION primary_signal.guard_revision_child()")
    op.execute("DROP FUNCTION primary_signal.guard_story_revision()")
    op.execute("REVOKE USAGE ON SCHEMA primary_signal FROM primary_signal_public_owner")
