# ADR 0003: Feed entry, article, content version, and story identity

Status: Proposed

## Context

Feeds, web documents, fetched revisions, and real-world developments have
different identity and change rules. Treating an RSS GUID as an article key
loses cross-feed identity. Updating extracted text in place loses provenance.
Treating each article as a story preserves duplicate coverage rather than
organising it.

## Decision

Model four distinct entities:

- A **feed entry** is one item as observed in one feed. Its identity is scoped
  to that feed and preserves the reported GUID, URL, metadata, and observation
  times.
- An **article** is the stable source document reached from one or more feed
  entries or discovered URLs. It preserves submitted URLs, redirect history,
  and a conservative canonical identity. A feed GUID is evidence, not the
  article's global identity.
- A **content version** is an immutable fetched and extracted revision of an
  article. Normalised content hashes identify unchanged revisions; fetch
  attempts remain separately auditable.
- A **story** represents one real-world development and groups articles through
  versioned memberships with history and provenance.

Store source publication time, first seen, last seen, fetched, and processed
times separately. Automated identity and clustering decisions must be
traceable. Manual merges, splits, reassignments, and identity corrections take
precedence over later automated processing until explicitly released.

Downstream analysis refers to the exact content versions and story membership
generation used as input. It does not silently follow mutable current content.

## Consequences

- Re-polling can be idempotent without erasing feed-specific observations.
- Corrections and changed articles retain a usable provenance trail.
- Story clustering can improve independently of article retrieval.
- The model requires explicit reconciliation rules for redirects, canonical
  tags, syndicated copies, and URL changes.
- Membership and accepted-version pointers add complexity but avoid rewriting
  history.
- Removal and retention policy must account for references from published
  material to immutable versions.

## Deferred questions

- Which URL normalisation rules are safe enough for automatic article matches?
- When may canonical metadata merge two existing article identities?
- How should syndicated or materially duplicated documents be represented?
- Which content changes are material enough to create a new version?
- What retention and deletion rules apply to raw responses and extracted text?
