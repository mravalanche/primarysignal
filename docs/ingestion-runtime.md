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
termination during feed and article fetches; container restart and production
network isolation remain separate checks.
