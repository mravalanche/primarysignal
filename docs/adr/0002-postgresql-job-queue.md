# ADR 0002: PostgreSQL job queue and measured triggers for Redis

Status: Proposed

## Context

Ingestion and processing must survive restarts, avoid duplicate work, expose
failure history, and coordinate state changes with successor jobs. PostgreSQL
is already the system of record. Adding Redis in the first release would add a
second durable operational dependency before there is evidence that PostgreSQL
cannot meet the workload.

## Decision

Use PostgreSQL as the durable job queue for v1. A job records its type, payload,
queue, priority, deduplication key, run-after time, attempt limits, lifecycle
timestamps, bounded failure detail, worker identity, lease, and heartbeat.

Workers claim jobs in short transactions using row locking that skips work
already claimed by another worker. They commit the lease before performing
work and recover jobs whose leases expire. Work must be idempotent. A state
change and any successor job it requires are written in the same database
transaction.

Polling is the baseline wake-up mechanism. PostgreSQL notifications may reduce
latency, but are hints only; correctness must not depend on their delivery.
Retries use bounded exponential backoff with jitter and retain attempt history.

Do not add Redis in v1. Reconsider it only when production measurements show a
queue problem that cannot reasonably be corrected through query, index, worker,
or polling changes. Relevant evidence includes sustained claim contention,
unacceptable wake-up latency at an agreed polling load, or queue throughput
that consumes a material share of database capacity.

## Consequences

- Job state and the data it advances can be committed atomically.
- Backup, recovery, diagnostics, and local operation have one fewer service.
- Workers must keep claim transactions short and never process work while
  holding row locks.
- Queue tables need deliberate indexing, retention, and contention monitoring.
- PostgreSQL notifications cannot replace polling or lease recovery.
- If Redis is later introduced, durable job truth remains in PostgreSQL unless
  a new ADR replaces this decision.

## Deferred questions

- What queue latency and throughput targets define acceptable v1 performance?
- How long should completed jobs and attempt history be retained?
- What lease and heartbeat defaults are appropriate for each job type?
- Which measurements and thresholds would trigger a Redis evaluation?
