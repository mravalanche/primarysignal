# Isolated retrieval development proof

The optional `uv run --locked --offline poe isolated-retrieval-smoke` task
checks the real retriever and processors in a disposable Docker topology.
It uses synthetic content and namespace-local endpoints. It is not a production
deployment or a live-source test.

## Boundary under test

Processors have a database network and a dedicated internal RPC network. The
retriever has neither database network membership nor database credentials.
It uses the fixed development/test RPC profile described in
[the ingestion runtime](ingestion-runtime.md#fixed-fixture-rpc-profile).
No service publishes a host port.

A dedicated namespace owner installs and verifies one named nftables table
before the retriever starts. Only that owner receives `NET_ADMIN`. Workloads
and probes drop all capabilities. The owner changes its own namespace only;
it does not mount the Docker socket, use host networking, modify host firewall
rules, or flush Docker's rules. Unsupported capabilities fail the exercise.

The policy defaults to denying output, denies IPv6, permits only the explicit
fixture DNS path and intended HTTP ports, and rejects special-purpose IPv4
destinations. Loopback is not a blanket exception. The resolver configuration
is generated for the fixture; the proof does not trust Docker's embedded DNS
forwarding as evidence of a constrained resolver path.

## Safe synthetic probes

The fixture owns its source, DNS and forbidden sink endpoints. Addresses used
to represent public, private or metadata classes are assigned only inside its
network namespace. Before a probe connects, the runner must verify exact alias
ownership and local loopback routing, and confirm that each probe shares the
policy owner's current network namespace. It never probes an operator's LAN, host
services, real metadata endpoint or production database.

Direct TCP/UDP probes exercise the kernel rules independently of application
URL validation. Rule counters and sink observations distinguish enforced
denial from an unavailable service. Permitted synthetic DNS and HTTP exercise
the real retriever's resolution, address validation and pinned transport, then
the feed/article handlers and searchable inventory.

## Limits

Namespace-local routing verifies enforcement and RPC/data flow without making
external requests. It does not prove transit egress, public DNS, live sources,
production readiness or a target deployment's network policy. Those checks
remain open in the milestone ledger. Production startup remains disabled.

The runner uses private temporary fixtures, an explicit local Docker daemon,
fixed synthetic credentials and tracked build inputs. It removes only its own
containers, networks, volume, images and temporary files. It does not read
operator configuration or retain article text, credentials or operational logs
in repository artifacts.

## Verified development evidence

`uv run --locked --offline poe isolated-retrieval-smoke` passed on 2026-10-28.
Runtime image:
`sha256:b5feeaec8c490997e8b44e84dd65acde8a582044261a6dc4e918ef9159035f5c`.
Policy tooling image:
`sha256:ee041db35007c5301ae1963dbd9bc8862503491348e84937438502eda4300830`.

The run verified permitted fixture DNS over UDP/TCP, HTTP on port 80 and TCP
connections on port 443. Synthetic forbidden address classes, Docker stub DNS,
other ports and IPv6 were denied with namespace/route checks, kernel counters
and sink observations. The real retriever and separate feed/article workers
persisted one searchable content version. Private and mixed DNS answers and a
redirect to a forbidden target returned the expected API rejection without
forbidden sink traffic.

For managed restart, the runner stopped every dependent workload before
restarting the same policy owner. It verified semantic rule reapplication,
current namespace joins and resumed actual RPC retrieval, with the database
snapshot unchanged. This does not establish arbitrary crash resilience.
All owned containers, networks, volume, images and temporary files were removed.
No external requests were made; TCP port 443 permission is not an HTTPS or
public-transit verification.

## Bounded transit verification

The separate `uv run --locked --offline poe retrieval-transit-smoke` task
explicitly enables a transit-example fixture profile. The default synthetic
task still makes no external requests. Only the policy owner gains the
project-owned egress bridge; processors and PostgreSQL remain internal.
RPC exceptions are tied to the verified RPC interface. Approved upstream DNS
and established public replies are tied to the verified egress interface.

The credential-free DNS fixture forwards only `example.com` IN A/AAAA queries
to literal `1.1.1.1`, using bounded, transaction-matched UDP/TCP requests. It
preserves both answer families; the application still validates every answer,
and the kernel still denies IPv6. It does not use an operator resolver,
Docker's DNS stub, or a private fallback. Unreachable public DNS stops the run.

After the synthetic deny/pipeline/restart checks, the runner verifies actual
DNS and egress routing, then invokes the unchanged packaged retrieval API for
`https://example.com/`. A passing result requires normal certificate, hostname
and peer checks, HTTP 200, the exact final URL and no redirects, plus nonlocal
reply-counter evidence. The normal bounded redirect policy is retained; the
fixture DNS name filter is not a hard connection-level hostname boundary.
Unexpected results fail the proof. No example content is stored in PostgreSQL
or repository artifacts.

This verifies a controlled development transit path. It does not ingest a live
feed, enable production startup, select production DNS policy or establish a
target deployment's readiness.

The explicit transit task passed on 2026-10-28. Full synthetic enforcement,
data-flow and managed-restart checks ran first with zero upstream DNS activity.
The subsequent phase verified UDP/TCP traffic to `1.1.1.1`, faithful A/AAAA
answers, all-answer policy validation, and nonlocal IPv4 routes through the
owned egress interface. The real API returned HTTP 200 for the exact HTTPS
example URL with an empty redirect chain, bounded extraction metadata and
nonlocal established-reply counter evidence. The database snapshot remained
unchanged; no example body was retained.

Verified runtime image:
`sha256:11080be4fcc02377a497eaa27d3ccb998db22193c08e39bd4bed4434b0248315`.
Verified policy image:
`sha256:f914df369469eda1bbbef45742214649be94df294c39aab57743e153c3be7b89`.
All owned resources were removed. This verifies the development fixture's
transit path, not a production deployment or any live feed.
