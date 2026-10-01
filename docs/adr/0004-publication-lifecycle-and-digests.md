# ADR 0004: Publication lifecycle and immutable digest semantics

Status: Proposed

## Context

Routine stories should be able to publish automatically, but invalid generated
content and unsupported high-consequence claims must not reach readers.
Editors also need durable corrections, suppression, and replacement without
losing the record of what was published. A dated briefing must remain a stable
edition rather than changing whenever its underlying stories change.

## Decision

Use an explicit hybrid publication lifecycle for publishable story revisions:

- **draft**: incomplete or not yet approved for validation;
- **validated**: all automated gates pass and any required editorial approval
  is recorded;
- **published**: visible through the public projection;
- **suppressed**: deliberately excluded from current public presentation;
- **superseded**: retained for history but replaced by a newer revision or
  editorial decision.

Only validated revisions may become published. Routine content may make that
transition automatically when policy permits. Schema-invalid output never
validates. High-consequence evidence claims require qualifying deterministic
evidence or explicit editorial approval. Publication and suppression events
record actor, time, reason, policy version, and the accepted input artifacts.

Corrections create a new revision and supersede the old one; they do not mutate
the published revision in place. Manual corrections and publication decisions
are not overwritten by reprocessing.

A digest is assembled as a draft edition. Publishing it creates an immutable
dated edition whose ordered items point to the exact published story revisions,
headlines, summaries, placement, and provenance shown at that time. Later story
updates do not alter that edition. A correction or replacement is a new digest
revision linked to the prior edition, with a visible reason and timestamp.

## Consequences

- Readers and operators can reconstruct what was published and why.
- Automatic publication remains possible without bypassing evidence gates.
- Published briefings are reproducible even as stories develop.
- Corrections require new revisions and explicit supersession, increasing the
  number of retained records.
- Public queries must distinguish current story presentation from historical
  digest snapshots.
- Cache invalidation and search indexing must follow publication events rather
  than mutable working records.

## Deferred questions

- Which fields constitute a story revision, and which metadata may change
  without creating one?
- What timezone and editorial cut-off define a digest's publication date?
- How should public correction notices and withdrawn editions be presented?
