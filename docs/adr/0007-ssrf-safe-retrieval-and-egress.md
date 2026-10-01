# ADR 0007: SSRF-safe retrieval and deployment egress enforcement

- **Status:** Proposed
- **Date:** 2026-10-01

## Context and threats

Feed and article URLs are attacker-controlled input even when sources are
curated. Retrieval can otherwise be abused to reach the host, private services,
cloud metadata, or other network devices. DNS rebinding and redirect pivots can
defeat validation performed separately from the connection. Parsers and the
optional 13ft fallback also process hostile content and may be compromised.

The retriever crosses from the application into the public internet. It is not
trusted with PostgreSQL, Ollama, admin access, host services, or deployment
credentials. The processor may request retrieval through a narrow internal API,
but it has no general internet egress. The 13ft process is a second independent
retrieval boundary, not a trusted extension of caller-side checks.

## Decision

### URL and address validation

- Accept only absolute `http` and `https` URLs, initially on ports 80 and 443.
  Reject user-info, malformed or empty hosts, IPv6 zone IDs, non-canonical IP
  literals, ambiguous numeric encodings, and unexpected control characters.
- Canonicalise DNS names once, using strict IDNA handling, before policy checks.
  Keep the original canonical hostname for HTTP `Host`, TLS SNI, and certificate
  verification.
- Resolve the hostname before connecting. Reject the request if any A or AAAA
  answer is not globally routable unicast. Mixed public/private answer sets fail
  as a whole. The deny policy includes loopback, private, link-local, multicast,
  unspecified, documentation, reserved, benchmark, carrier-grade NAT, metadata,
  and IPv4-mapped IPv6 ranges.
- Base the range policy on a reviewed, versioned IANA special-purpose registry
  snapshot rather than the host platform's idea of `is_global` alone.

### Connection pinning

- The HTTP transport receives the validated address set and connects only to an
  address from that set. It must not perform another hostname lookup.
- Preserve hostname-based TLS verification and SNI. After connecting, verify
  that the peer address is the selected validated address. A retry may use
  another address only from the same validated set.
- Treat each redirect as a new request: parse, resolve, validate, and pin again.
  Cap redirects and reject HTTPS-to-HTTP downgrade by default. Never forward
  authorization, cookies, or other origin credentials across a redirect.
- Disable environment proxies, `.netrc`, ambient credentials, and persistent
  cookies. Retrieval never invokes a shell.
- Apply independent DNS validation, connection pinning, redirect checks, and
  resource limits inside 13ft because it performs its own connections. The
  fallback remains disabled until its implementation passes the same tests.

### Resource and API limits

- Bound connection, per-read, and total time; redirect count; response headers;
  compressed and decompressed bytes; extraction work; concurrent requests; and
  global and per-origin request rates.
- Stream to the limit and abort before buffering an oversized response. Treat
  declared content length as a hint, not proof of size.
- The internal retrieval API accepts a URL and bounded retrieval options only.
  It cannot select arbitrary proxy settings, headers, files, or destinations.
  Restrict reachability to a dedicated network containing only the processor
  and retriever, bind only on that network, and apply request-size and
  concurrency limits.
- Redact URL queries and user-info from logs. Do not normally log response bodies
  or extracted article text.

### Deployment egress boundary

- Put processor/retriever and retriever/13ft communication on dedicated internal
  networks. Do not publish retriever, 13ft, PostgreSQL, or Ollama ports on the
  host.
- Enforce egress outside the containers with host firewall or equivalent
  platform policy. First allow only the named service-to-service edges and
  approved DNS resolver endpoints. Then reject host, LAN, metadata, and other
  special-purpose destinations before allowing public TCP 80/443 for the
  retriever and 13ft. The processor may reach only PostgreSQL, retriever, and
  constrained Ollama; PostgreSQL and Ollama have no runtime internet egress.
- Block host, LAN, metadata, and all non-global or special-purpose destinations
  at the egress layer. If equivalent IPv6 policy cannot be enforced, disable
  IPv6 for these networks instead of leaving an unfiltered path.
- Containers run without `NET_ADMIN` or `NET_RAW`, with dropped capabilities,
  `no-new-privileges`, read-only filesystems, and no Docker socket or broad host
  mounts. Apply CPU, memory, process, and restart limits. Application validation
  remains required even when firewall policy is present.
- Deployment must fail closed if the egress policy cannot be installed or
  verified. A generic Docker network is not considered egress enforcement.

## Consequences

Connection pinning prevents DNS rebinding between validation and use. Denying a
whole mixed answer set is deliberately stricter than choosing only a public
answer. Some legitimate sites with unusual DNS, ports, redirects, or split
horizon addressing will fail and require an explicit source-policy decision;
they must not receive a global bypass.

The firewall contains parser or 13ft compromise even if application URL checks
are bypassed. It adds deployment-specific configuration and requires tests from
inside each container. Disabling ambient proxy behaviour means installations
that require an outbound proxy need a separately reviewed design.

## Verification criteria

- Unit and integration tests cover private and special IPv4/IPv6 ranges,
  IPv4-mapped IPv6, mixed DNS answers, alternate encodings, zone IDs, user-info,
  forbidden ports, rebinding, redirect pivots, and downgrade redirects.
- A controlled DNS test proves the address used for the connection is the
  validated address and that the connected peer is checked.
- TLS tests prove certificate verification uses the requested hostname while
  the socket is pinned to the approved address.
- Proxy environment variables, `.netrc`, cookies, and hostile redirect headers
  cannot affect retrieval.
- Compressed/decompressed size, header, timeout, concurrency, and redirect caps
  fail closed without retaining partial content as a valid version.
- Equivalent tests exercise direct retrieval and 13ft independently.
- In-container probes prove every forbidden edge in the network matrix fails,
  including host, LAN, metadata, PostgreSQL, Ollama, admin, and general internet
  access from processes that do not require it.
- Tests run with IPv4 and IPv6, or verify that IPv6 is disabled at the boundary.

## User decisions required

Before deployment work is accepted, the operator must choose, without recording
live infrastructure details in this repository:

1. The deployment egress mechanism (for example host `nftables` or an equivalent
   platform policy) and who owns its installation and persistence.
2. The approved DNS resolver endpoints and whether DNS is direct or provided by
   a constrained local resolver.
3. Whether any source may use non-standard ports or HTTPS-to-HTTP redirects.
   The proposed v1 answer is no; exceptions require a separate, source-scoped
   review.
4. Concrete timeout, byte, redirect, extraction, and concurrency limits, based
   on measurements against the reference corpus.
