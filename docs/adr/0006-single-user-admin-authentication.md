# ADR 0006: Single-user admin authentication, sessions, and CSRF

- **Status:** Proposed
- **Date:** 2026-10-01

## Context and threats

The v1 administration surface has one operator. It can publish, suppress, and
reprocess content and change source and ranking configuration. Network location
is not proof of identity: another device, a malicious page, or a compromised
service on the same network could reach it.

The main threats are password guessing, session theft or fixation, cross-site
request forgery, unsafe proxy trust, and accidental exposure of administrative
routes through the public application. A compromised admin session is expected
to have broad editorial impact, so its lifetime and reach must be bounded.

The trust boundary is the admin HTTPS origin. The browser and admin application
are inside it after authentication. The public site, other network clients,
forwarded proxy headers, and all fetched or generated content are untrusted.

## Decision

### Authentication

- Use one local password. Do not add an identity provider or treat source IP as
  authentication in v1.
- Store only an Argon2id PHC password hash, supplied to the application through
  deployment configuration outside the repository. Parameters are explicit and
  versioned. Benchmark them on the deployment host before release; verification
  should take roughly 250–750 ms without unsafe memory pressure.
- The login form accepts the password only; there is no discoverable account
  name in v1. Do not log credentials, password hashes, cookies, or CSRF tokens.
- Rate-limit login attempts by source and across the whole service. Use bounded
  delays rather than permanent account lockout, which would create a simple
  denial of service against the sole operator.
- Require authentication for every admin page and API except login and a
  minimal unauthenticated liveness response. Logout is an authenticated,
  CSRF-protected mutation. The public application factory must not register
  admin routes.

### Sessions

- Use server-side sessions. The browser receives a cryptographically random,
  opaque 256-bit identifier; the database stores only its HMAC-SHA-256 digest,
  keyed with a deployment secret held outside the database and repository.
- Rotate the identifier after login and any privilege-relevant change. Revoke it
  on logout. Changing the password or the session secret revokes all sessions.
- Enforce both idle and absolute expiry on the server. Do not rely on cookie
  expiry alone. Expired and unknown identifiers fail closed.
- Use a host-only cookie named `__Host-primary_signal_admin` with `Secure`,
  `HttpOnly`, `SameSite=Strict`, and `Path=/`; do not set `Domain`.
- Serve the admin origin over HTTPS in production. The admin and public origins
  remain distinct, for example `admin.example.test` and `public.example`.

### CSRF and request validation

- Every state-changing request uses `POST`, `PUT`, `PATCH`, or `DELETE`; `GET`
  and `HEAD` have no side effects.
- Bind a random CSRF secret to each session. Require it in a form field or
  request header and compare it in constant time.
- For state-changing requests, also require `Origin` to match the configured
  admin origin exactly. A missing, malformed, or mismatched origin is rejected.
- Allowlist the request host. Trust forwarding headers only from explicitly
  configured proxy addresses and only after the direct peer is verified as a
  trusted proxy.
- Require an explicit confirmation step for destructive or broad actions.
  Duplicate submissions remain safe through idempotency or deduplication.

## Consequences

This avoids an external identity dependency and keeps authentication suitable
for one operator. Database-backed sessions permit immediate revocation and keep
bearer credentials out of storage, at the cost of a database lookup and session
cleanup.

`SameSite=Strict` reduces CSRF risk but may require the operator to sign in
after following a link from another site. HTTPS and an admin certificate become
deployment requirements. Password recovery is an operator-run hash replacement,
not an email flow.

## Verification criteria

- Route-manifest tests prove the public application has no admin routes.
- Every protected admin route rejects missing, expired, revoked, and malformed
  sessions; login rotates a pre-existing identifier.
- Mutations reject missing or invalid CSRF tokens, cross-origin requests,
  unapproved hosts, and spoofed forwarding headers.
- `GET` and `HEAD` requests do not change durable state.
- Cookie tests assert the exact security attributes and absence of `Domain`.
- Rate-limit tests cover repeated attempts across one source and many sources,
  without creating a permanent lockout.
- Logs and error responses contain no submitted password, session identifier,
  CSRF token, password hash, or form body.
- A production-mode startup check fails if HTTPS/proxy/origin settings are
  incomplete or contradictory.

## User decisions required

Before implementation is accepted, the operator must choose, in private
deployment configuration rather than this repository:

1. The admin origin and how its HTTPS certificate is issued and renewed.
2. Idle and absolute session lifetimes. Proposed defaults are 30 minutes idle
   and 12 hours absolute.
3. Login rate thresholds and the trusted proxy addresses. Proposed defaults are
   five failed attempts per 15 minutes per source and 20 globally, followed by
   bounded backoff.
4. The operator procedure for generating and replacing the Argon2id hash and
   session secret without placing either in source control or shell history.
