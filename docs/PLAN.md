# Primary Signal — Product and Delivery Plan

Status: product baseline; implementation in progress
Date: 2026-10-01

## 1. Product definition

Primary Signal is a self-hosted, evidence-led cyber-security briefing and
publication. It turns reporting from a curated set of sources into a concise
daily briefing and a searchable stream of developing stories.

It is not a conventional RSS reader and it is not a full threat-intelligence
platform. Its distinguishing unit is the **story**: a cluster of source
articles about the same underlying development, with provenance, evidence,
ranking, and a concise synthesis.

### Primary users

1. **Owner/operator** — tunes sources and policy, monitors system health, and
   may correct exceptional results without operating a routine review queue.
2. **Public reader** — understands what changed, why it matters, and where the
   evidence came from without reading duplicate coverage.

### Product promise

> Cybersecurity, reduced to signal.

A reader should be able to identify the most material developments in under
five minutes and reach the strongest original evidence within two interactions.

## 2. Proposed v1 operating model

Use an **automation-first hybrid publication policy**:

- routine stories may publish automatically after all validation gates pass;
- failed or schema-invalid AI output never publishes;
- high-consequence evidence claims publish only with deterministic
  authoritative evidence or an optional scoped editorial override;
- the operator can suppress, correct, merge, split, re-rank, and reprocess;
- manual corrections are durable and protected from automated overwrite.

Items that fail validation are withheld without blocking eligible stories or
dated briefings. They are grouped for optional diagnosis and rule tuning; the
operator is not expected to clear a routine review queue.

Publication states should be explicit and auditable:

```text
draft -> validated -> published
  |          |           |
  +------> suppressed <--+
                 |
              superseded
```

The exact transition rules are an ADR and must be agreed before schema work.

## 3. V1 scope

### Included

- 15–30 curated RSS/Atom sources with database-backed configuration.
- Idempotent polling, conditional requests, retries, source health, and fetch
  history.
- Conservative URL identity and immutable extracted-content versions.
- Direct HTTP retrieval with readable-text extraction.
- Explicit per-source 13ft fallback, disabled by default.
- Postgres-backed durable jobs with leases, deduplication, retry history, and
  dead-job visibility.
- Staged Ollama analysis with strict schemas, input fingerprints, provenance,
  caching, and bounded repair attempts.
- Conservative story clustering with manual merge, split, and reassignment.
- Deterministic, versioned story ranking with visible factor contributions.
- Evidence-backed signals with definitions and stored supporting evidence.
- PostgreSQL full-text search.
- A responsive public publication and a distinct local administration shell.
- Light and dark themes, shared design tokens, and WCAG 2.2 AA core flows.
- Operational logging, metrics, database backup, and tested restoration.

### Not included

- A universal RSS reader or arbitrary user-added feeds.
- Perfect semantic clustering or autonomous evidence adjudication.
- Personalised feeds, accounts, subscriptions, email, push, or social posting.
- SIEM integration, IOC case management, or a general CTI platform.
- Browser automation as the default retrieval method.
- Redis, Elasticsearch, Kubernetes, or microservices without measured need.
- Multiple admin users or collaborative editorial workflows.
- Republishing fetched full article bodies on the public site.

## 4. Information architecture and taxonomy

### Public navigation

- **Today** — dated daily briefing, lead stories, developing stories, latest.
- **Latest** — reverse-chronological clustered story stream.
- **Deep Reads** — substantive original research and analysis.
- **Topics** — topic directory and filtered streams.
- **Search** — clustered results with linkable filters.

Top Stories and Developing are homepage treatments or filters, not permanent
navigation destinations. UK and Strategic are lenses, not categories.

### Facets

Keep these dimensions separate:

- **Primary topic:** Vulnerabilities & Exploitation; Threat Activity &
  Incidents; Security Engineering; Policy & Strategy; Research & Tools.
- **Story type:** News; Research; Advisory; Incident; Analysis; Opinion; Tool
  Release.
- **Lens/relevance:** Technical depth; Strategic significance; UK relevance.
- **Tags/entities:** CVEs, vendors, products, actors, technologies, sectors.
- **Evidence/status:** Primary Source; Official Advisory; Active Exploitation;
  Exploit Available; Actionable; Confirmed Incident; Developing; Widely
  Reported; Deep Read.

Use one primary topic and only a small number of secondary topics. Begin AI
Security, Cloud & Identity, and Supply Chain as curated tags until volume proves
they warrant first-class topic pages.

Do not display High Signal as a badge; express it through ranking and placement.
Defer public Unverified labelling until its rules and editorial implications are
defined.

## 5. Public experience

The public unit is a **story cluster**, not an individual article.

### Default story card

1. Primary-topic kicker.
2. Headline.
3. Two-line Primary Signal synthesis.
4. Source count and first/latest reporting time.
5. At most two important evidence/status markers.

Tags, detailed scores, model metadata, reading time, and the complete evidence
set belong on the story page.

### Story page

- concise synthesis and “Why it matters”;
- first seen and latest material update;
- evidence/status with plain-language definitions;
- source trail identifying the primary and best technical sources;
- material-update timeline where useful;
- topics/entities and related stories;
- prominent outbound links to original material;
- clear attribution where sources conflict or facts remain uncertain.

Add a public Methodology page covering clustering, generated summaries,
ranking, evidence markers, corrections, and provenance.

## 6. Administration experience

Use a separate shell with no edit controls embedded in public pages:

- **Overview:** worker state, freshness, queue failures, source health.
- **Sources:** configuration, cadence, strategy, weight, recent failures.
- **Content:** stories, underlying articles, publication state, corrections.
- **Processing:** jobs, runs, retries, failure history, reprocessing scope.
- **AI & prompts:** active models, prompt versions, timings, validation errors.
- **Diagnostics:** feed, fetch, extraction, worker, and system history.
- **Settings:** ranking and taxonomy only once editing is safe and versioned.

Every asynchronous action must show pending, success, partial failure, and
failure states, and must prevent accidental duplicate actions.

## 7. Technical architecture

Keep one modular Python codebase and one image with explicit entrypoints:

```text
src/primary_signal/
  config/
  db/
  sources/
  ingestion/
  retrieval/
  analysis/
  stories/
  publication/
  jobs/
  web/
    common/
    public/
    admin/
  entrypoints/
```

Entrypoints:

- `migrate` — one-shot Alembic migration task;
- `web --surface public` — public routes only;
- `web --surface admin` — public plus admin routes;
- `processor --queues ...` — durable jobs, PostgreSQL, and constrained Ollama;
- `retriever` — a narrow internal retrieval/extraction service with public
  internet access but no PostgreSQL or Ollama access;
- `scheduler` — inserts due jobs but performs no processing.

FastAPI, Jinja2, HTMX, Tailwind, and DaisyUI remain appropriate. Prefer normal
synchronous SQLAlchemy sessions for database work and bounded async HTTP for
retrieval. Do not add an AI gateway or Redis in v1.

Use separate public and admin app factories and a route-manifest test proving
that the public application contains no admin routes.

Use the same codebase/image for retriever and processor, but run them as
separate constrained processes. The processor has no general internet egress;
the retriever has no database or AI access. This limits the impact of hostile
content or parser compromise without introducing a separately maintained
microservice codebase.

## 8. Data model principles

Keep these identities distinct:

- **Feed entry:** what a feed reported.
- **Article:** a source document and its stable identity.
- **Content version:** one immutable fetched/extracted revision.
- **Story:** the real-world development grouping articles.

Core entities:

- sources, feeds, feed poll runs, feed entries;
- articles, content versions, fetch attempts;
- model definitions, prompt versions, processing runs, embeddings;
- topics, tags/entities, and their relationships;
- stories, memberships, and membership history;
- signal definitions, assertions, and evidence;
- score snapshots and ranking-policy versions;
- generated summaries, digests, and digest items;
- jobs and versioned settings/audit history.

Important rules:

- store published, first-seen, last-seen, fetched, and processed timestamps
  separately as `timestamptz`;
- do not equate RSS GUIDs with canonical article identity;
- preserve original URLs and redirect chains;
- version content by normalized hash;
- make AI outputs append-only and point to a current accepted run;
- protect manual memberships, signals, and taxonomy corrections;
- store rank components and policy version with every total;
- use one embedding model/dimension per indexed generation.

## 9. Durable job model

PostgreSQL is sufficient for v1. Jobs require:

- type, payload, queue, priority, and dedupe key;
- status, run-after time, attempts, and maximum attempts;
- worker identity, lock lease, heartbeat, and lease recovery;
- bounded error code/detail and complete lifecycle timestamps.

Claim work in a short transaction with `FOR UPDATE SKIP LOCKED`, set a lease,
commit, and perform the work outside the transaction. Jobs must be idempotent.
Enqueue successor work transactionally with the durable state change that
requires it. Use exponential backoff with jitter and preserve retry history.

## 10. Pipeline

```text
discover -> fetch -> extract -> deterministic facts -> embed
         -> classify -> cluster -> score -> summarize -> publish
```

Stages are independently retryable and derive readiness from successful runs
and input fingerprints rather than one mutable article-state field.

For curated v1 sources, fetch newly discovered eligible articles before adding
LLM metadata triage. Add triage only when measured volume or hardware pressure
justifies the false-negative risk.

An AI-run fingerprint includes content/member hashes, schema version, prompt
version, model identity and parameters, and relevant configuration version.
Changed prompts/models enqueue only affected stages and downstream dependants.
Old accepted output remains live until its replacement validates.

## 11. Clustering, ranking, and signals

### Conservative clustering

1. Match exact evidence such as canonical URLs or advisory identifiers.
2. Generate candidates by time window, title/text search, shared entities, and
   embedding neighbours.
3. Score pairs using title similarity, embedding similarity, entity/CVE
   overlap, time proximity, and source relationships.
4. Auto-merge only above a high-confidence threshold; leave the grey band
   separate or queued for review.

Avoid single-link transitive clustering. Compare candidates with a stable story
anchor/centroid, preserve membership history, and honour manual decisions.

### Ranking

Rank stories, then choose representative sources. Store normalized factors for
technical depth, strategic significance, UK relevance, source quality,
original research, evidence strength, novelty, recency, and independent
corroboration. Avoid counting popularity/corroboration multiple times.

### Signals

An LLM may propose signals and supporting spans, but high-consequence evidence
claims require deterministic validation against authoritative evidence or
manual approval. Every public assertion stores its definition, scope, evidence,
observed time, producing rule/run, confidence, and override history.

## 12. Design system baseline

- Source Serif 4 for editorial headlines; Source Sans 3 for body and UI;
  monospace only for CVEs, timestamps, and technical metadata.
- Warm paper/ink neutrals with one restrained blue-teal accent in both themes.
- Semantic red/amber/green reserved for state, never decoration.
- 4px spacing scale; 72rem page maximum; 44rem readable prose measure.
- Thin borders, 8–12px radii, minimal shadow, elevation only for overlays.
- Shared primary, secondary, quiet, and danger button variants.
- At least 44px touch targets; visible focus; status never conveyed by colour
  alone.
- Topics are typographic kickers, tags are neutral chips, evidence/status is
  icon plus text with a discoverable definition.

Implement semantic CSS variables and shared components over DaisyUI rather than
styling individual pages independently.

## 13. Delivery milestones

### M0 — Product contract and reference corpus

- Ratify audience, publication policy, taxonomy, terminology, and signal rules.
- Assemble roughly 150 representative articles across 25–40 real stories.
- Label expected clusters, primary sources, topics, signals, and relevance.
- Record initial ADRs and threat model.

Exit: expected behaviour is testable against a shared corpus.

### M1 — Platform and ingestion foundation

- Package skeleton, configuration, migrations, structured logging, Compose.
- Sources, feeds, scheduler, Postgres jobs, retries, and diagnostics.
- Direct retrieval, extraction, content versioning, and SSRF controls.

Exit: a restart-safe, searchable inventory exists without AI dependency.

### M2 — Useful non-AI product slice

- Publication states and editorial controls.
- Public Latest and story pages, Postgres search, source trails.
- Admin source/content/processing views.

Exit: selected stories can be published and read end to end.

### M3 — AI enrichment and ranking

- Model/prompt registry, strict schemas, summaries, classification, embeddings.
- Versioned deterministic ranking and bounded reprocessing.

Exit: validated summaries and reproducible ordering operate on the corpus.

### M4 — Stories, evidence, and briefings

- Conservative clustering, manual corrections, signals/evidence.
- Immutable dated Daily Briefings and archive.

Exit: the product delivers a trustworthy daily reading experience.

### M5 — Release hardening

- Public/admin isolation, database roles, network controls, container hardening.
- Accessibility/manual focus review, responsive testing, load/timeout limits.
- Backup, clean restore exercise, operational documentation.

Exit: all release gates pass and deployment is recoverable.

## 14. Cross-cutting acceptance criteria

- Re-polling an unchanged feed creates no duplicate articles or active jobs.
- Processing is restart-safe and expired leases recover automatically.
- Invalid AI output is rejected, retried according to policy, and visible.
- Unchanged inputs are not reprocessed unless a dependency version changes.
- Every public summary links to its sources and has internal provenance.
- Every ranking is reproducible from stored factors and a policy version.
- Cluster precision on the reference corpus is at least 90%; false joins are
  treated as more harmful than missed joins.
- Manual corrections survive automated reprocessing.
- Evidence-backed labels cannot publish without qualifying stored evidence.
- Public runtime has no admin routes and no access to raw bodies, prompts, jobs,
  or unrestricted mutation.
- Search/filter state is linkable, reversible, and retained on navigation.
- Public/admin core flows meet WCAG 2.2 AA and work from 320px to 1440px.
- No standard story card shows more than two evidence/status markers.
- Empty, loading, stale, partial-data, error, and retry states are implemented.
- A documented backup restores successfully into a clean database.

## 15. Decisions required before implementation

1. Ratify the hybrid publication model and the high-consequence signal gates.
2. Define the single-user v1 admin authentication/session configuration.
   Mutating admin routes require authentication even on the LAN.
3. Define backup RPO/RTO and the off-host destination (without placing secrets
   in the repository).
4. Record the legal/editorial policy for stored article bodies, excerpts, and
   per-source 13ft usage.
5. Choose the first evaluation corpus and measurable quality thresholds.
6. Benchmark candidate Ollama models on the actual host before selecting them.
7. Specify host/firewall egress enforcement compatible with the deployment
   environment.

## 16. ADR backlog

1. Modular monolith and multi-entrypoint image.
2. PostgreSQL job queue and measured triggers for Redis.
3. Feed-entry/article/content-version/story identity model.
4. Hybrid publication lifecycle and immutable digest semantics.
5. Public data projection and separate database roles.
6. Single-user admin authentication, session, and CSRF policy.
7. SSRF-safe connection pinning and host-level egress policy.
8. Per-source 13ft policy.
9. Embedding model/dimension and migration method.
10. Conservative clustering thresholds and manual overrides.
11. Ranking policy and evidence requirements for each signal.
12. Versioned AI artifacts and dependency-aware reprocessing.
13. Raw HTML/feed retention and deletion policy.
14. Backup RPO/RTO, encryption, retention, and restore drills.

## 17. Immediate next step

The database schema, durable queue, feed scheduler, and controlled synthetic
scheduler-to-entry path are in place. Local article retrieval and bounded
plain-text extraction use the isolated retriever. An injectable retrieval job
handler now records attempts and immutable extracted-text versions. An internal
read-only inventory searches the current version of each article through
PostgreSQL; the local operator command returns metadata only. Production
retrieval remains unbound. Verify deployment egress controls, service
readiness, source-disable serialization, and extracted-text retention before
live polling is enabled. Present editorial UI mock-ups for approval before
expanding the M2 public or administration pages.

The M0 reference corpus, signal evidence matrix, and open operator decisions
remain separate work before automated publication can be evaluated.

## 18. V1 security baseline

The following are release requirements rather than later hardening.

### Admin

- Require a local Argon2id password and server-side session for mutating admin
  routes; do not introduce an identity provider for the single-user v1.
- Use LAN HTTPS in production and a host-only `__Host-` cookie with `Secure`,
  `HttpOnly`, and `SameSite=Strict`.
- Require CSRF tokens plus Origin validation for state changes; mutations never
  use GET.
- Rotate sessions after login, bound expiry, rate-limit login, allowlist hosts,
  and trust only explicitly configured proxies.
- Store the password hash and session-signing secret outside the repository.

### Retrieval and SSRF

- Permit only HTTP/HTTPS and initially only ports 80/443.
- Reject malformed/user-info hosts, ambiguous address encodings, IPv6 zone IDs,
  and any target whose A/AAAA set contains a non-global address.
- Pin the validated address to the connection while preserving hostname/SNI and
  verify the connected peer. Do not validate and then allow a client-side
  second DNS lookup.
- Revalidate every redirect; cap redirects, headers, compressed/decompressed
  body size, extraction work, and total time.
- Disable environment proxies, `.netrc`, cookies, and cross-origin credential
  forwarding. Never invoke URLs through a shell.
- Apply the same controls independently inside 13ft; caller-side validation is
  insufficient when 13ft performs its own DNS resolution.

### Network and data boundaries

```text
cloudflared -> public web only
public web  -> published database views only
admin web   -> application database only
processor   -> database, retriever, constrained Ollama inference
retriever   -> DNS, public 80/443, isolated 13ft
13ft        -> DNS and public 80/443 only
Ollama      -> no runtime internet egress
PostgreSQL  -> no outbound access
```

Enforce the matrix with host firewall policy as well as Docker networks. Do not
publish PostgreSQL, Ollama, 13ft, or retriever ports on the host.

Use distinct database roles for migrations, public reads, admin DML, processor,
scheduler, and backup. Revoke default schema privileges. Each process asserts
its expected role on startup. Public access is limited to curated published
views that exclude raw text, prompts, model output, internal errors, operator
notes, jobs, and unpublished/deleted records.

### Untrusted output and containers

- Keep Ollama non-agentic: no tools, shell, HTTP, filesystem, database, Docker,
  or home-system access.
- Treat model output as untrusted after schema validation. Preserve Jinja
  autoescaping, never render fetched/model HTML as safe, and avoid remote
  article images in v1.
- Use a restrictive CSP and `rel="noopener noreferrer"` on external links.
- Run production containers as non-root with read-only filesystems, explicit
  tmpfs, `no-new-privileges`, dropped capabilities, default seccomp, and
  resource/PID/time limits. Never mount the Docker socket or broad host paths.
- Lock dependencies and pin base images. Parse XML with DTDs and external
  entities disabled.
- Redact URL queries, user-info, cookies, authorization, credentials, and form
  bodies from logs. Do not normally log raw articles, prompts, or model output.

### Security release gates

1. Public route enumeration proves admin/internal endpoints return 404.
2. Unauthenticated admin and cross-origin/CSRF mutations fail safely.
3. Runtime role assertions pass; public cannot read base tables or mutate data.
4. SSRF tests cover loopback, private/link-local/metadata ranges, IPv6 and
   mapped IPv6, mixed DNS answers, redirect pivots, alternate encodings, DNS
   rebinding, proxy variables, ports, and decompression/size attacks.
5. In-container probes prove every forbidden edge in the network matrix fails.
6. 13ft cannot reach PostgreSQL, Ollama, admin, host services, metadata, or LAN.
7. Ollama is not host-published and exposes only required inference operations.
8. Stored-XSS tests cover malicious feed/article/model content in both shells.
9. Fetch, queue, search, database, and Ollama resource-abuse limits hold.
10. Secret scanning finds no credentials in Git history, images, rendered
    Compose configuration, logs, or error responses.
11. Container identity, filesystem, capability, and mount controls are verified.
12. An encrypted backup restores cleanly and least-privilege roles are retested.
