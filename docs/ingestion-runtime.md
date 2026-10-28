# Feed and article runtime

This is a controlled development foundation. Live polling and public deployment
controls remain open.

## Feed scheduler

The scheduler queues due feed polls and does no network work itself. It runs
continuously by default, or performs one pass for operational checks:

```sh
uv run primary-signal-scheduler
uv run primary-signal-scheduler --once
```

It uses the `PRIMARY_SIGNAL_DATABASE_*` connection settings and its own
`PRIMARY_SIGNAL_SCHEDULER_*` tuning settings. The defaults are a batch of 100,
a 30-second idle poll interval, and equal-jitter database retries starting
between half a second and one second, capped at 60 seconds. The database login
must hold the queue-submit and feed-scheduling capabilities described in
[database roles](database-roles.md). `SIGINT` and `SIGTERM` stop idle waits
immediately and let an in-flight bounded database operation
finish before the process exits. A statement timeout applies to each statement,
not the whole batch, so production wiring also needs a whole-pass time budget
and shutdown grace that covers it.

## Feed processor foundation

The ingestion processor has lease renewal, expired-job recovery, bounded
database retries, and atomic completion callbacks. Feed parsing and poll-record
persistence are available for synthetic tests and an injected fetcher. A local
processor CLI can bind feed or article handlers only with an explicit
development or test environment and a loopback retriever origin. Production
startup fails closed. Live polling depends on isolated retrieval and its
network controls; the processor must not fetch internet URLs directly.

## Feed and article retrieval foundation

The retriever library validates feed URLs and every DNS answer against a dated
special-purpose address policy. Its HTTP transport connects to a validated
address, checks the peer, preserves hostname-based TLS verification, rechecks
redirects, and limits response headers and body size. Synthetic tests exercise
these rules. A fixed feed-fetch API is available through
`primary-signal-retriever --allow-local-fetch` on loopback when
`PRIMARY_SIGNAL_ENVIRONMENT` is explicitly `development` or `test`. Its processor
client uses a fixed configured origin with no ambient proxy or redirects and a
bounded response. The feed handler can be bound for controlled integration
tests; production processor startup fails closed, and the retriever command
rejects production startup until deployment network controls are ready.
DNS callers have a finite wait and a shared limit on simultaneous system
lookups. The network egress controls described in
[ADR 0007](adr/0007-ssrf-safe-retrieval-and-egress.md) remain required before
live polling can be enabled.

The local retriever also has a fixed article-fetch endpoint. It applies the
same address and redirect policy, extracts bounded plain text from HTML inside
the database-free retriever, and returns content hashes and redirect history.
Raw HTML is discarded there. An explicit, injectable article job handler now
records fetch attempts and immutable extracted-text versions in PostgreSQL.
Repeated content reuses the same version. Live article retrieval remains
disabled until deployment egress controls and service readiness are verified.
Source-disable serialization and the fixed-policy extracted-text cleanup have
isolated PostgreSQL tests; their target-deployment checks remain separate.

## Internal article inventory

The article inventory reads only each article's current extracted version. It
uses PostgreSQL English-language full-text search and returns source, title,
date, and word-count metadata. From a local terminal with a dedicated inventory
database login configured through `PRIMARY_SIGNAL_DATABASE_URL` and
`PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`, run:

```sh
uv run primary-signal-article-inventory --search "synthetic notice" --limit 20
```

The JSON output includes an opaque cursor for the next page, but no extracted
body or raw URL. This is an internal operator command, not a public story
search endpoint. The public and administration sites do not expose this reader.

## Source and feed health

From a local terminal, use a dedicated database login with only the
`primary_signal_cap_source_health` capability. Set
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`
for that login, then run:

```sh
uv run primary-signal-source-health --limit 100
```

The JSON report lists at most the requested number of sources and feeds each,
ordered by source key and then feed name and ID. Truncation flags show when more
rows exist. Feed status is `disabled` when the feed or its source is disabled;
`stale` when it has failures, has never succeeded and is due, or its last
success is more than two poll intervals old; and `healthy` otherwise. The
report contains names and polling timestamps, but no configured URLs, error
details, job payloads, or article content. It is a local operator command.

## Retriever readiness

The retriever exposes `GET /v1/ready` on its internal API. Its fixed response
identifies protocol version 1 and the feed and article fetch capabilities. The
probe performs no retrieval, DNS lookup or database access, and does not consume
fetch admission capacity. It returns no deployment configuration.

Before recovering leases or claiming a job, the local processor checks that the
configured retriever responds with the capability required by its queue. The
probe uses the same fixed origin and disables ambient proxies and redirects.
The probe accepts at most 1 KiB of JSON and has a five-second overall network
deadline and a two-second idle timeout, separate from feed and article fetches.
Cancellation covers response headers and chunk framing as well as body reads.
An unavailable or incompatible retriever stops startup without consuming a job
attempt. Once startup succeeds, ordinary job retries still handle later
retriever failures.

Readiness establishes API availability and compatibility. Deployment network
isolation, source reachability and spare fetch capacity need separate checks.
Production processor and retriever startup remain disabled.

## Abrupt worker recovery

The disposable PostgreSQL gate includes a processor crash-recovery test in
`tests/test_processor_entrypoint_postgres.py`. It starts the actual local
processor CLI against a synthetic loopback retriever, waits until a feed or
article fetch is in flight, then kills that child process. The feed scenario
checks that no poll result or discovered article commits before recovery. The
article scenario checks that no fetch attempt, content version, current-version
pointer or searchable result appears before recovery. The test expires the
abandoned lease, starts a replacement processor, and checks that the work
commits once and the old lease cannot complete the job. Re-fetching identical
article content must reuse the same immutable version. Lease expiry and retry
scheduling are moved forward explicitly so the test does not wait for production
timeout intervals.

Run the PostgreSQL-enabled gate with:

```sh
bash scripts/dev_test_env.sh --skip-apt --offline
```

The gate creates and removes its own database. The crash test requires
`PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE=true` as well as the test database
settings. It uses no live feeds or production services. This verifies process
termination during feed and article fetches. The optional smoke below verifies
article-worker container recovery separately; production network isolation
remains unverified.

## Container crash recovery smoke

With Docker and the development environment installed, run:

```sh
uv run --locked --offline poe ingestion-recovery-smoke
```

This optional task runs separate feed and article cases, each with a fresh
project and PostgreSQL volume. It builds the packaged runtime from tracked
inputs. A credential-free synthetic retriever holds a response while the runner
kills the exact worker container. The article worker has queue-consume and
article-persist capabilities. The feed worker has queue-submit, queue-consume
and feed-poll capabilities so discovery can atomically enqueue retrieval. A
separate fixture setup login seeds the synthetic job and advances only its
expired lease and retry time.

To run one case directly, use
`uv run --locked --offline python -m scripts.smoke_ingestion_recovery --mode feed`
or select `--mode article`. The module defaults to the article case; the Poe
task selects both.

The runner checks that the interrupted fetch committed no observation, content
version, current-version pointer or searchable result. It restarts the same
database and worker containers, checks the database and volume identities,
rejects the abandoned lease through the restricted queue API, and requires one
searchable content version after article recovery. The feed case instead
requires one poll result, entry, discovered article and canonical URL, plus
exactly one queued article-retrieval job. Feed discovery must not create a
content version or searchable result before that job runs.

The retriever fixture owns the worker's network namespace and exposes only a
literal loopback endpoint. The disposable network is internal and has no host
ports. It is a controlled fake responder, not the production retriever. The
smoke does not verify live-source retrieval or production network isolation.
It reads no operator configuration and removes only its own containers,
network, volume, image and temporary files.

The real smoke passed on 2026-10-28 with local runtime image
`sha256:35bfd9d0da8c83de1ca6887b4002986dfe4992de8aa1acf11a2096bd6f933a4d`.
It observed SIGKILL exit 137, no partial state, recovery history
`lease_expired` then `succeeded`, rejected the old lease before and after
completion, and verified exactly one searchable content version. The same
database container, volume and database system identity survived restart; the
retriever namespace owner remained running. All owned resources were removed.
The combined feed/article task also passed on 2026-10-28. The feed case
verified zero partial poll, entry, article, URL or successor state before
recovery, then exactly one successful poll and linked source lineage with one
untouched queued retrieval job. Its abandoned lease was rejected before and
after completion. Both cases retained database/container identities and
removed their independent resources.

The combined run's article image was
`sha256:a35b781502826a3110265626b7c73e3f4215b46ecc0a611e7a3e20bee05e666e`;
its feed image was
`sha256:1762d76adbb262e5d27047235d1d03bb04580818e8188fbe3c1607b6a6781c13`.
Together with the controlled re-poll/dedupe and timeout/retry checks in
`tests/test_ingestion_end_to_end_postgres.py`, these runs complete M3's
re-poll/failure/retry/restart exit check. Live-source ingestion, production
bindings and deployment egress verification remain open.

## Queue and job health

Use a dedicated database login with only `primary_signal_cap_job_health` and
configure `PRIMARY_SIGNAL_DATABASE_URL` and
`PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE` for the local command:

```sh
uv run primary-signal-job-health --queue ingestion --status running --limit 100
```

The report contains per-queue counts and recent job metadata. Queues are sorted
by name; jobs are sorted by creation time and ID, newest first. The default
limit is 100, with a maximum of 500 queues and 500 jobs. Separate truncation
flags show when additional rows exist. Omit the filters to report all queues
and job states. Valid queue names that have no jobs return an empty report.

Queue counts include every status even when `--status` filters the job rows.
`due` means queued work whose scheduled time has arrived; `delayed` means
queued work scheduled later. `queued_retries` counts queued jobs with a prior
attempt. `expired` means running jobs whose lease has expired. These conditions
use one database timestamp recorded as `as_of`. Counts and rows are sampled
operational state; they can change between queries. A heartbeat timestamp does
not prove a worker is alive.

The reader never selects payloads, deduplication keys, worker identities,
lease tokens, error text or attempt history. It cannot submit, claim, retry or
cancel jobs. The output limit bounds returned rows, not the history scanned by
aggregate counts; the normal database statement timeout still applies.
Deployment logins and membership remain operator-owned. The disposable test
gate provisions only synthetic test roles.

## Pause and resume a source

The local source-control command changes one configured source by its exact
key, as shown by the source-health report. Use a separate database login with
only `primary_signal_cap_source_control` and the ordinary
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`
settings:

```sh
uv run primary-signal-source-control pause example-advisories --reason "Pause collection for review"
uv run primary-signal-source-control resume example-advisories --reason "Collection review complete"
```

Each actual transition records its previous and new state, database time,
reason and authenticated database actor in private, append-only audit history.
The reason is required, trimmed and limited to 500 characters; control
characters and blank reasons are rejected. JSON output contains only the
source key, enabled state, change flag, update time and audit-event ID. Repeating
the current state succeeds without another event or a changed update time.
Unknown source keys fail without a transition.

Pausing serializes with source eligibility checks. A scheduler already inside
its eligible transaction may finish before pause returns. After the pause
commits, scheduling skips the source and leaves its next-poll times intact.
Jobs that loaded their target before pause may still start or finish retrieval
externally. Their observations are discarded when persistence finds the source
still paused. Jobs loaded while paused perform no retrieval. Other sources can
continue processing.

Resuming restores ordinary scheduling at the existing due times. Individual
feed flags, cadence and conditional-request validators retain their settings.
Discarded results and terminal jobs stay in their current state; subsequent
polling supplies eligible new work. Existing public stories retain their
publication state, while future publication decisions still check source
eligibility.

The capability executes one fixed state-transition function. It has no direct
source, feed, job, content or audit-table access. Login configuration and
capability membership remain deployment-owned.
