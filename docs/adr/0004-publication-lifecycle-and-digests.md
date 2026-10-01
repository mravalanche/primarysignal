# ADR 0004: Publication lifecycle and immutable digest semantics

Status: Proposed

## Context

Routine stories should publish automatically without an editorial queue, while
invalid generated content and unsupported high-consequence claims must not
reach readers. The operator needs durable optional controls for tuning,
correction, suppression, and replacement. A dated briefing must remain a
stable edition rather than changing whenever its underlying stories change.

## Decision

Use an explicit hybrid publication lifecycle for publishable story revisions:

- **draft**: incomplete, held, or failed validation; unpublished and not
  awaiting mandatory review;
- **validated**: all automated gates pass for the current input and policy
  versions;
- **published**: visible through the public projection;
- **suppressed**: deliberately excluded from current public presentation;
- **superseded**: retained for history but replaced by a newer revision or
  editorial decision.

Only validated revisions may become published. Routine content makes that
transition automatically when policy permits. Schema-invalid output never
validates. High-consequence evidence claims require qualifying deterministic
evidence; otherwise the claim is omitted or the affected story is withheld.
An editor may make a scoped exception, but unresolved candidates never block
other stories or a daily briefing. Publication and suppression events record
actor, time, reason, policy version, and the accepted input artifacts.

Withheld revisions, failed proposals, and uncertain associations are
diagnostics, not assigned editorial work. Transient failures retry
automatically. Permanent failures and stale proposals are grouped, retained for
a bounded period, and suppressed or expired by policy. Normal publication
continues without acknowledgement.

Corrections create a new revision and supersede the old one; they do not mutate
the published revision in place. Manual corrections and publication decisions
are not overwritten by reprocessing.

The scheduler assembles and publishes a dated digest automatically from
eligible published story revisions at the configured cut-off. Invalid or held
items are omitted and do not block the edition. The resulting edition is
immutable and records the exact revisions, headlines, summaries, placement,
and provenance shown at that time. Later updates do not alter it. A correction
or replacement creates a linked digest revision with a visible reason and
timestamp.

## Consequences

- Readers and operators can reconstruct what was published and why.
- Automatic publication is the normal operating mode; operator intervention is
  optional.
- Failed candidates require no routine editorial action and do not block
  eligible work.
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
