# ADR 0010: Extracted-text retention

Status: Accepted policy; implementation pending
Date: 2026-10-09

## Context

Primary Signal stores extracted article text for search, reprocessing, and
evidence review. It does not store raw HTML or raw feed bodies. Content versions
also hold hashes, timestamps, and source links that support an audit trail. The
text needs a finite lifetime when it no longer serves a current article or
published story.

## Decision

- Keep extracted text while its version is the article's current version.
- Keep extracted text for every version cited by a story revision that has been
  published, including a story later suppressed or replaced. This preserves
  the evidence behind historical publication decisions.
- When a version is superseded and has no protected publication reference,
  remove its extracted text 90 days after its latest transition out of current
  status. If it becomes current again, reset that clock.
- Keep the version's identifiers, hashes, extraction metadata, and fetch
  history after its text is removed. A cleared version is unavailable for
  full-text search and reprocessing. A repeat fetch of identical content must
  create a new, text-bearing version rather than reactivate the cleared one.
- Honour a specific removal request through a separate, reviewed operator
  procedure. Backup expiry and restoration must follow the same policy before
  production collection begins.

This policy does not authorize live retrieval by itself. Production retrieval
still needs verified egress controls and a tested retention mechanism.

## Implementation requirements

The current schema makes `content_versions.extracted_text` non-null and forbids
all updates and deletes. Implement the policy with a migration that permits a
one-way text removal through a narrow database capability. Do not grant broad
`UPDATE` or `DELETE` on content versions to the processor, web processes, or
public reader.

Record the latest transition out of current status separately from the
immutable version metadata. Backfill existing noncurrent versions
conservatively from migration time. Change same-hash deduplication so only
text-bearing versions can be reused; a cleared version retains its identity
and hashes without blocking a new fetch of the same content.

Cleanup must lock the article and candidate version, then recheck the current
pointer, age, and historical publication references inside the transaction.
Publication must require text on every cited version and lock those versions
before the status transition. If cleanup wins first, publication rejects the
stale draft and requires a fresh cited version. If publication wins first,
cleanup leaves the text in place. Test both orders with disposable PostgreSQL.

Run cleanup in bounded batches through a dedicated maintenance login with
execute-only access to a fixed-policy database function. Record version IDs
and counts without logging article text. A dry-run inventory should show
eligible versions before deletion is enabled. Backup expiry and restoration
must not silently restore text that has already been removed.

## Consequences

Search and model reprocessing can use current text. Historical published
evidence remains available for audit. Unused old text stops accumulating after
the 90-day period, while non-text provenance remains intact. Published text may
be retained for the life of the audit record, so the operator needs a separate
reviewed removal path and backup retention schedule.
