# ADR 0001: Modular monolith and multi-entrypoint image

Status: Proposed

## Context

Primary Signal needs several runtime processes with different duties and trust
boundaries: public web, admin web, processing, retrieval, scheduling, and
migrations. Splitting these into independently maintained services would add
deployment and versioning overhead before the product boundaries are proven.
Running everything as one process would make least-privilege network and data
access difficult to enforce.

## Decision

Maintain one modular Python codebase and build one versioned application image.
Expose explicit entrypoints for:

- one-shot database migrations;
- public web;
- admin web;
- durable-job processors;
- retrieval and extraction;
- scheduling.

Modules own their domain behaviour and expose narrow interfaces. Entrypoints
compose those modules; they do not become separate implementations.

Run entrypoints as separate processes with only the network, database, and
filesystem access each duty requires. In particular, retrieval can reach the
public internet but cannot reach PostgreSQL or Ollama, while processors can
reach PostgreSQL, the retrieval service, and constrained Ollama inference but
have no general internet egress.

Public and admin web applications use separate app factories. The public
factory must not register admin routes.

## Consequences

- A single build and release keeps shared models, schemas, and policies in step.
- Process isolation supports least privilege without the maintenance cost of
  multiple repositories or separately versioned services.
- Module boundaries require discipline because the language runtime does not
  enforce them.
- A defect in shared code may affect several entrypoints, so release testing
  must exercise each one.
- The image contains code unused by some processes; runtime permissions, not
  image contents alone, remain the security boundary.

## Deferred questions

- What internal protocol will the processor use to request retrieval work?
- Which module dependencies should be checked automatically in CI?
- What independent scaling or reliability evidence would justify extracting a
  module into a separately released service?
