# ADR 0009: PostgreSQL persistence foundation

Status: Accepted
Date: 2026-10-01

## Context

Primary Signal needs durable ingestion history, article identity, immutable
content versions, and a job queue. These records depend on PostgreSQL locking,
constraints, JSONB, time-zone-aware timestamps, and database-enforced
provenance. A reduced SQLite path would leave the important behaviour untested.

The public repository must describe a safe target architecture without exposing
live credentials or deployment details. Raw upstream payloads also introduce
storage and rights questions that are unnecessary for the first pipeline.

## Decision

- Support PostgreSQL 18 for v1. Use synchronous SQLAlchemy 2 and Alembic. Do not
  add a SQLite compatibility layer.
- Generate UUIDv7 identifiers in Python and store all operational timestamps as
  `timestamptz`.
- Use versioned, conservative URL normalisation: lowercase and IDNA-normalise
  the scheme and host, remove fragments and default ports, and preserve path and
  query content. Retain both original and normalised URLs.
- Store extracted article text in immutable content versions so processing can
  be reproduced and audited. Do not store raw HTML responses or raw feed bodies.
- Keep migration credentials separate from runtime credentials. Runtime roles
  and the public projection are introduced with the components that consume
  them; production login names and secret handling remain deployment-owned.
- Test migrations and PostgreSQL-only integrity rules against a disposable
  PostgreSQL service in CI.

## Consequences

- Development and CI need PostgreSQL for integration checks.
- URL and content identity can evolve through explicit version fields without
  rewriting earlier observations.
- Re-extraction from the exact raw response is unavailable. A later retention
  policy may add encrypted, bounded raw storage through a separate decision.
- Extracted text requires a documented retention and deletion policy before a
  production deployment handles live sources.
