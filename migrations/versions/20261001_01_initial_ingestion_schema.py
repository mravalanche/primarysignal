"""Create the initial ingestion and job schema."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE primary_signal.sources (
 id uuid PRIMARY KEY, source_key text NOT NULL CONSTRAINT uq_sources_source_key UNIQUE CHECK (source_key ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'), name text NOT NULL,
 homepage_url text NOT NULL, enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE primary_signal.feeds (
 id uuid PRIMARY KEY, source_id uuid NOT NULL REFERENCES primary_signal.sources(id) ON DELETE RESTRICT, name text NOT NULL,
 configured_url text NOT NULL, normalized_url text NOT NULL, url_hash text NOT NULL CHECK (char_length(url_hash)=64), url_normalization_version integer NOT NULL CHECK (url_normalization_version>0),
 enabled boolean NOT NULL DEFAULT true, poll_interval_seconds integer NOT NULL DEFAULT 900 CHECK (poll_interval_seconds>=60), next_poll_at timestamptz,
 etag text, last_modified text, last_attempt_at timestamptz, last_success_at timestamptz, consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures>=0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), CONSTRAINT uq_feeds_url_normalization_version_url_hash UNIQUE(url_normalization_version,url_hash),
 CHECK (last_success_at IS NULL OR last_attempt_at IS NULL OR last_success_at<=last_attempt_at)
);
CREATE INDEX ix_feeds_source_id ON primary_signal.feeds(source_id);
CREATE INDEX ix_feeds_due ON primary_signal.feeds(next_poll_at,id) WHERE enabled;
CREATE TABLE primary_signal.jobs (
 id uuid PRIMARY KEY, job_type text NOT NULL, payload_version integer NOT NULL CHECK(payload_version>0), payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object') CHECK(octet_length(payload::text)<=65536),
 queue text NOT NULL DEFAULT 'default', priority integer NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100), deduplication_key text,
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','dead','cancelled')), run_after timestamptz NOT NULL,
 attempt_count integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5 CHECK(max_attempts>0), worker_id text, lease_expires_at timestamptz, heartbeat_at timestamptz,
 first_started_at timestamptz, completed_at timestamptz, last_error_code text, last_error_detail text CHECK(last_error_detail IS NULL OR char_length(last_error_detail)<=2048),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), CHECK(attempt_count BETWEEN 0 AND max_attempts),
 CHECK((status='running' AND worker_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status<>'running' AND worker_id IS NULL AND lease_expires_at IS NULL)),
 CHECK((status IN ('succeeded','dead','cancelled') AND completed_at IS NOT NULL) OR (status IN ('queued','running') AND completed_at IS NULL))
);
CREATE INDEX ix_jobs_claim ON primary_signal.jobs(queue,priority DESC,run_after,id) WHERE status='queued';
CREATE INDEX ix_jobs_recovery_lease ON primary_signal.jobs(lease_expires_at) WHERE status='running';
CREATE INDEX ix_jobs_status_created ON primary_signal.jobs(status,created_at);
CREATE UNIQUE INDEX uq_jobs_active_deduplication ON primary_signal.jobs(queue,job_type,deduplication_key) WHERE deduplication_key IS NOT NULL AND status IN ('queued','running');
CREATE TABLE primary_signal.job_attempts (
 id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES primary_signal.jobs(id) ON DELETE RESTRICT, attempt_number integer NOT NULL CHECK(attempt_number>0), worker_id text NOT NULL,
 status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','succeeded','retry','dead','lease_expired','cancelled')), started_at timestamptz NOT NULL, finished_at timestamptz,
 lease_expires_at timestamptz NOT NULL, error_code text, error_detail text CHECK(error_detail IS NULL OR char_length(error_detail)<=2048), created_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_job_attempts_job_id_attempt_number UNIQUE(job_id,attempt_number), CHECK((finished_at IS NULL AND status='running') OR (finished_at IS NOT NULL AND status<>'running')), CHECK(finished_at IS NULL OR finished_at>=started_at)
);
CREATE INDEX ix_job_attempts_job_started ON primary_signal.job_attempts(job_id,started_at);
CREATE TABLE primary_signal.feed_poll_runs (
 id uuid PRIMARY KEY, feed_id uuid NOT NULL REFERENCES primary_signal.feeds(id) ON DELETE RESTRICT, job_id uuid REFERENCES primary_signal.jobs(id) ON DELETE RESTRICT,
 requested_url text NOT NULL, status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','succeeded','not_modified','failed')), started_at timestamptz NOT NULL, completed_at timestamptz,
 http_status integer, entries_seen integer CHECK(entries_seen IS NULL OR entries_seen>=0), entries_discovered integer CHECK(entries_discovered IS NULL OR entries_discovered>=0),
 returned_etag text, returned_last_modified text, error_code text, error_detail text CHECK(error_detail IS NULL OR char_length(error_detail)<=2048), created_at timestamptz NOT NULL DEFAULT now(),
 CHECK((completed_at IS NULL AND status='running') OR (completed_at IS NOT NULL AND status<>'running')), CHECK(completed_at IS NULL OR completed_at>=started_at),
 CHECK(entries_discovered IS NULL OR entries_seen IS NULL OR entries_discovered<=entries_seen), CONSTRAINT uq_feed_poll_runs_feed_id_id UNIQUE(feed_id,id)
);
CREATE INDEX ix_feed_poll_runs_feed_started ON primary_signal.feed_poll_runs(feed_id,started_at);
CREATE TABLE primary_signal.articles (
 id uuid PRIMARY KEY, source_id uuid NOT NULL REFERENCES primary_signal.sources(id) ON DELETE RESTRICT, current_canonical_url_id uuid, current_content_version_id uuid,
 first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), CHECK(last_seen_at>=first_seen_at)
);
CREATE INDEX ix_articles_source_last_seen ON primary_signal.articles(source_id,last_seen_at);
CREATE TABLE primary_signal.feed_entries (
 id uuid PRIMARY KEY, feed_id uuid NOT NULL REFERENCES primary_signal.feeds(id) ON DELETE RESTRICT, first_poll_run_id uuid NOT NULL,
 article_id uuid REFERENCES primary_signal.articles(id) ON DELETE RESTRICT, identity_method text NOT NULL CHECK(identity_method IN ('guid','url','fingerprint')), identity_version integer NOT NULL CHECK(identity_version>0), identity_key text NOT NULL CHECK(char_length(identity_key)=64),
 reported_guid text, reported_url text, reported_title text, reported_summary text, reported_author text, reported_published_at timestamptz, reported_updated_at timestamptz,
 metadata_hash text NOT NULL CHECK(char_length(metadata_hash)=64), first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_feed_entries_feed_id_identity_version_identity_key UNIQUE(feed_id,identity_version,identity_key), CHECK(last_seen_at>=first_seen_at), CONSTRAINT fk_feed_entries_first_poll_same_feed FOREIGN KEY(feed_id,first_poll_run_id) REFERENCES primary_signal.feed_poll_runs(feed_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_feed_entries_feed_last_seen ON primary_signal.feed_entries(feed_id,last_seen_at);
CREATE INDEX ix_feed_entries_article_id ON primary_signal.feed_entries(article_id);
CREATE TABLE primary_signal.article_urls (
 id uuid PRIMARY KEY, article_id uuid NOT NULL REFERENCES primary_signal.articles(id) ON DELETE RESTRICT, original_url text NOT NULL, normalized_url text NOT NULL,
 normalized_url_hash text NOT NULL CHECK(char_length(normalized_url_hash)=64), normalization_version integer NOT NULL CHECK(normalization_version>0),
 kind text NOT NULL CHECK(kind IN ('submitted','redirect','canonical','canonical-hint')), first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), CONSTRAINT uq_article_urls_article_id_id UNIQUE(article_id,id), CONSTRAINT uq_article_urls_normalization_version_normalized_url_hash UNIQUE(normalization_version,normalized_url_hash), CHECK(last_seen_at>=first_seen_at)
);
CREATE INDEX ix_article_urls_article_kind ON primary_signal.article_urls(article_id,kind);
CREATE TABLE primary_signal.fetch_attempts (
 id uuid PRIMARY KEY, article_id uuid NOT NULL REFERENCES primary_signal.articles(id) ON DELETE RESTRICT, job_id uuid REFERENCES primary_signal.jobs(id) ON DELETE RESTRICT,
 retrieval_strategy text NOT NULL, requested_url text NOT NULL, final_url text, redirect_chain jsonb NOT NULL CHECK(jsonb_typeof(redirect_chain)='array'),
 status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','fetched','not_modified','rejected','failed')), started_at timestamptz NOT NULL, completed_at timestamptz,
 http_status integer, content_type text, byte_count bigint CHECK(byte_count IS NULL OR byte_count>=0), returned_etag text, returned_last_modified text,
 resulting_content_version_id uuid, error_code text, error_detail text CHECK(error_detail IS NULL OR char_length(error_detail)<=2048), created_at timestamptz NOT NULL DEFAULT now(),
 CHECK((completed_at IS NULL AND status='running') OR (completed_at IS NOT NULL AND status<>'running')), CHECK(completed_at IS NULL OR completed_at>=started_at), CONSTRAINT uq_fetch_attempts_article_id_id UNIQUE(article_id,id)
);
CREATE INDEX ix_fetch_attempts_article_started ON primary_signal.fetch_attempts(article_id,started_at);
CREATE TABLE primary_signal.content_versions (
 id uuid PRIMARY KEY, article_id uuid NOT NULL REFERENCES primary_signal.articles(id) ON DELETE RESTRICT, origin_fetch_attempt_id uuid NOT NULL CONSTRAINT uq_content_versions_origin_fetch_attempt_id UNIQUE,
 raw_response_hash text NOT NULL CHECK(char_length(raw_response_hash)=64), normalized_content_hash text NOT NULL CHECK(char_length(normalized_content_hash)=64), normalization_version integer NOT NULL CHECK(normalization_version>0),
 extracted_title text, extracted_text text NOT NULL, source_published_at timestamptz, extractor_name text NOT NULL, extractor_version text NOT NULL, content_type text, language text,
 word_count integer CHECK(word_count IS NULL OR word_count>=0), fetched_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_content_versions_article_id_id UNIQUE(article_id,id), CONSTRAINT uq_content_versions_article_id_normalization_version_no_1bda UNIQUE(article_id,normalization_version,normalized_content_hash), CONSTRAINT fk_content_versions_origin_fetch_same_article FOREIGN KEY(article_id,origin_fetch_attempt_id) REFERENCES primary_signal.fetch_attempts(article_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_content_versions_article_fetched ON primary_signal.content_versions(article_id,fetched_at);
ALTER TABLE primary_signal.articles ADD CONSTRAINT fk_articles_current_url_same_article FOREIGN KEY(id,current_canonical_url_id) REFERENCES primary_signal.article_urls(article_id,id) ON DELETE RESTRICT;
ALTER TABLE primary_signal.articles ADD CONSTRAINT fk_articles_current_content_same_article FOREIGN KEY(id,current_content_version_id) REFERENCES primary_signal.content_versions(article_id,id) ON DELETE RESTRICT;
ALTER TABLE primary_signal.fetch_attempts ADD CONSTRAINT fk_fetch_attempts_result_same_article FOREIGN KEY(article_id,resulting_content_version_id) REFERENCES primary_signal.content_versions(article_id,id) ON DELETE RESTRICT;
CREATE FUNCTION primary_signal.reject_immutable_history_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable history cannot be changed'; END; $$;
CREATE TRIGGER protect_content_versions BEFORE UPDATE OR DELETE ON primary_signal.content_versions FOR EACH ROW EXECUTE FUNCTION primary_signal.reject_immutable_history_change();
CREATE FUNCTION primary_signal.reject_terminal_history_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF TG_OP='DELETE' OR OLD.status<>'running' OR NEW.status='running' THEN RAISE EXCEPTION 'attempt history only permits running to terminal finalization'; END IF; IF (to_jsonb(NEW)-ARRAY['status','completed_at','finished_at','http_status','entries_seen','entries_discovered','returned_etag','returned_last_modified','final_url','redirect_chain','content_type','byte_count','resulting_content_version_id','error_code','error_detail']::text[]) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','completed_at','finished_at','http_status','entries_seen','entries_discovered','returned_etag','returned_last_modified','final_url','redirect_chain','content_type','byte_count','resulting_content_version_id','error_code','error_detail']::text[]) THEN RAISE EXCEPTION 'attempt identity cannot be changed during finalization'; END IF; RETURN NEW; END; $$;
CREATE TRIGGER protect_feed_poll_runs BEFORE UPDATE OR DELETE ON primary_signal.feed_poll_runs FOR EACH ROW EXECUTE FUNCTION primary_signal.reject_terminal_history_change();
CREATE TRIGGER protect_fetch_attempts BEFORE UPDATE OR DELETE ON primary_signal.fetch_attempts FOR EACH ROW EXECUTE FUNCTION primary_signal.reject_terminal_history_change();
CREATE TRIGGER protect_job_attempts BEFORE UPDATE OR DELETE ON primary_signal.job_attempts FOR EACH ROW EXECUTE FUNCTION primary_signal.reject_terminal_history_change();
CREATE FUNCTION primary_signal.preserve_feed_entry_identity() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (NEW.feed_id,NEW.first_poll_run_id,NEW.identity_method,NEW.identity_version,NEW.identity_key,NEW.reported_guid,NEW.reported_url,NEW.reported_title,NEW.reported_summary,NEW.reported_author,NEW.reported_published_at,NEW.reported_updated_at,NEW.metadata_hash,NEW.first_seen_at) IS DISTINCT FROM (OLD.feed_id,OLD.first_poll_run_id,OLD.identity_method,OLD.identity_version,OLD.identity_key,OLD.reported_guid,OLD.reported_url,OLD.reported_title,OLD.reported_summary,OLD.reported_author,OLD.reported_published_at,OLD.reported_updated_at,OLD.metadata_hash,OLD.first_seen_at) OR NEW.last_seen_at<OLD.last_seen_at THEN RAISE EXCEPTION 'first-observed identity is immutable and last_seen_at cannot move backwards'; END IF; RETURN NEW; END; $$;
CREATE TRIGGER preserve_feed_entry_first_values BEFORE UPDATE ON primary_signal.feed_entries FOR EACH ROW EXECUTE FUNCTION primary_signal.preserve_feed_entry_identity();
"""


def upgrade() -> None:
    statement_lines: list[str] = []
    inside_dollar_quote = False
    for line in DDL.splitlines():
        statement_lines.append(line)
        if line.count("$$") % 2:
            inside_dollar_quote = not inside_dollar_quote
        if line.rstrip().endswith(";") and not inside_dollar_quote:
            statement = "\n".join(statement_lines).strip()
            op.execute(statement[:-1])
            statement_lines.clear()
    if any(line.strip() for line in statement_lines):
        raise RuntimeError("initial schema DDL contains an unterminated statement")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE primary_signal.articles DROP CONSTRAINT fk_articles_current_url_same_article"
    )
    op.execute(
        "ALTER TABLE primary_signal.articles DROP CONSTRAINT fk_articles_current_content_same_article"
    )
    op.execute(
        "ALTER TABLE primary_signal.fetch_attempts DROP CONSTRAINT fk_fetch_attempts_result_same_article"
    )
    for table in (
        "feed_entries",
        "feed_poll_runs",
        "job_attempts",
        "article_urls",
        "content_versions",
        "fetch_attempts",
        "articles",
        "jobs",
        "feeds",
        "sources",
    ):
        op.execute(f"DROP TABLE primary_signal.{table}")
    op.execute("DROP FUNCTION primary_signal.preserve_feed_entry_identity()")
    op.execute("DROP FUNCTION primary_signal.reject_terminal_history_change()")
    op.execute("DROP FUNCTION primary_signal.reject_immutable_history_change()")
