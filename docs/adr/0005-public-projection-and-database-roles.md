# ADR 0005: Public projection and separate database roles

Status: Accepted (current-story projection implemented; digest projection pending)

## Context

The public service is internet-facing and needs only a small, read-only subset
of publication data. Base tables also contain unpublished material, raw text,
prompts, model output, processing errors, operator notes, jobs, and audit data.
Application-only filtering would make an exposed route or query bug sufficient
to cross that boundary.

## Decision

Make a curated public projection the database contract for the public web
application. It contains only currently published fields needed for public
pages and search, plus intentional immutable digest history. It excludes raw
article bodies, unpublished or deleted records, internal provenance detail,
prompts, model output, job data, errors, credentials, and operator notes.

The public application connects with a dedicated read-only database role. That
role can select from the public projection and cannot read base tables, call
unsafe functions, create objects, or mutate data. Public projection objects are
owned by a non-login owner role rather than by the public runtime role.

Use distinct least-privilege roles for:

- migrations and schema ownership;
- public reads;
- admin application reads and writes;
- processing workers;
- scheduling;
- backup and restore operations.

Revoke default schema privileges and grant access explicitly. Each process
asserts its expected role on startup. Schema migrations include privilege tests,
and route-manifest tests independently prove that the public application has no
admin routes.

The projection is a security boundary as well as an API. Changes to its fields
or eligibility rules require review and tests showing that unpublished and
internal data remain inaccessible.

## Current implementation

The first projection uses security-barrier views in `primary_signal_public`.
Every view follows `stories.current_revision_id` and requires that revision
to be published and the story not suppressed. A separate non-login view owner
has column-specific SELECT grants on the publication tables. The public reader
capability has SELECT only on the six projection views and USAGE only on the
projection schema. Its login must not inherit or be able to assume the owner
or migration role. The publication writer will validate public source URLs
with `PublicSource` before promoting a revision; the database checks URL
shape but does not implement the full host/IP policy. A source without an
ingested content version requires an explicit, auditable editorial exception
in the future writer.

The public-projection bootstrap creates its two non-login roles. A deployment administrator
must grant the migration login membership in `primary_signal_public_owner`
before this migration runs so it can assign view and schema ownership. The
public login receives only `primary_signal_cap_public_read`; neither login
credentials nor membership grants belong in this repository. PostgreSQL's
default `public` schema must not grant CREATE to that login. Any future
projection function needs an explicit EXECUTE revoke from PUBLIC.

The manual publication writer is a trusted backend-only foundation. Its
dedicated login must inherit only `primary_signal_cap_publication_write`; it
must not be a migration login or be shared with either web runtime. The
public web startup check rejects publication capabilities, and the current
admin web runtime has no database connection. The writer API records decisions
with a named actor and source-version fingerprint, but the database's direct
table grants do not force writes through that API. They do not enforce event
audit, fetched-version eligibility, or authenticated operator identity against
a writer login issuing direct SQL. A future authenticated service must bind
the actor to its session. No unattended automatic publication may use this
manual path; contract gates need an independently enforced implementation.

## Consequences

- A public application compromise has a smaller database blast radius.
- Publication eligibility is centralised and testable instead of repeated in
  route queries.
- Migrations must update projections, ownership, and grants carefully.
- Search and historical digest access must operate within the same restricted
  contract.
- Separate credentials and role checks add deployment work, but prevent one
  shared account from silently accumulating privileges.
- Database restrictions complement rather than replace route, network, and
  output-escaping controls.

## Deferred questions

- Should the projection use ordinary views, materialised views, copied
  publication tables, or a measured combination?
- What freshness target applies between publication and public visibility?
- Which provenance and correction fields are intentionally public?
- What minimum privileges does the backup role require for the chosen tooling?
- How will role and grant tests run in local development and CI?
