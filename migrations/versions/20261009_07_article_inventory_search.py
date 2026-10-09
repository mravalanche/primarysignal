"""Add indexed article text search behind a metadata-only reader function."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_07"
down_revision: str | Sequence[str] | None = "20261009_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_article_inventory"
FUNCTION_SIGNATURE = "primary_signal.search_article_inventory(text,text,timestamptz,uuid,integer)"
SEARCH_VECTOR = (
    "setweight(to_tsvector('english'::regconfig, "
    "coalesce(extracted_title, ''::text)), 'A'::\"char\") || "
    "setweight(to_tsvector('english'::regconfig, extracted_text), 'B'::\"char\")"
)


def upgrade() -> None:
    """Index extracted text; expose only bounded metadata through one function."""

    op.execute(
        "CREATE INDEX ix_content_versions_english_search "
        "ON primary_signal.content_versions USING gin ((" + SEARCH_VECTOR + "))"
    )
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    op.execute(
        """
CREATE FUNCTION primary_signal.search_article_inventory(
    p_search text, p_source_key text, p_cursor_at timestamptz,
    p_cursor_id uuid, p_limit integer
) RETURNS TABLE (
    article_id uuid, source_key text, source_name text, canonical_url text,
    title text, first_seen_at timestamptz, fetched_at timestamptz,
    word_count integer
) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
SET statement_timeout = '5s'
AS $primary_signal_inventory$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 51
       OR (p_search IS NOT NULL AND (
           char_length(p_search) > 200 OR btrim(p_search) = ''
           OR p_search ~ '[[:cntrl:]]'
       ))
       OR (p_source_key IS NOT NULL AND (
           char_length(p_source_key) > 128
           OR p_source_key !~ '^[a-z0-9]+(-[a-z0-9]+)*$'
       ))
       OR ((p_cursor_at IS NULL) <> (p_cursor_id IS NULL))
    THEN
        RAISE EXCEPTION 'invalid inventory query' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT a.id, s.source_key, s.name, u.normalized_url,
           cv.extracted_title, a.first_seen_at, cv.fetched_at, cv.word_count
    FROM primary_signal.articles AS a
    JOIN primary_signal.sources AS s ON s.id = a.source_id
    JOIN primary_signal.article_urls AS u ON u.id = a.current_canonical_url_id
    JOIN primary_signal.content_versions AS cv ON cv.id = a.current_content_version_id
    WHERE (p_source_key IS NULL OR s.source_key = p_source_key)
      AND (p_search IS NULL OR (
          setweight(to_tsvector('english'::regconfig,
              coalesce(cv.extracted_title, ''::text)), 'A'::"char") ||
          setweight(to_tsvector('english'::regconfig,
              cv.extracted_text), 'B'::"char")
      ) @@ plainto_tsquery('english'::regconfig, p_search))
      AND (p_cursor_at IS NULL OR (a.first_seen_at, a.id) < (p_cursor_at, p_cursor_id))
    ORDER BY a.first_seen_at DESC, a.id DESC
    LIMIT p_limit;
END
$primary_signal_inventory$;
"""
    )
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIGNATURE} TO {ROLE}")


def downgrade() -> None:
    """Remove the inventory function and the search index."""

    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION_SIGNATURE} FROM {ROLE}")
    op.execute(f"DROP FUNCTION {FUNCTION_SIGNATURE}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
    op.execute("DROP INDEX primary_signal.ix_content_versions_english_search")
