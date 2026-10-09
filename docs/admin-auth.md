# Admin authentication backend

The admin application is a separate web process. In development and tests,
an unconfigured admin app serves only liveness routes. Production startup
requires an HTTPS admin origin, a local Argon2id PHC password hash, and a
32-byte or longer session key supplied as hexadecimal. It also requires a
database login with only `primary_signal_cap_admin_session` membership.
The Reading desk also needs a second login with only
`primary_signal_cap_editorial_read` membership. Startup verifies both logins
before serving requests. The session login cannot read stories, and the
editorial login cannot manage sessions or article text.

The deployment-owned settings are `PRIMARY_SIGNAL_ADMIN_ORIGIN`,
`PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC`, and
`PRIMARY_SIGNAL_ADMIN_SESSION_KEY_HEX`. The usual
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`
identify the separate session-only login. Do not place their values in the
repository, shell history, or logs. The password hash and session key are
independent; replacing either invalidates existing sessions.
`PRIMARY_SIGNAL_ADMIN_EDITORIAL_DATABASE_URL` and
`PRIMARY_SIGNAL_ADMIN_EDITORIAL_DATABASE_EXPECTED_ROLE` identify the
editorial-read login. `PRIMARY_SIGNAL_ADMIN_PUBLIC_ORIGIN` is optional; when
set, it must be an HTTPS origin and enables a link to the current public story.
The public and admin origins must use different hostnames; different ports on
one hostname do not isolate a host-only cookie.

The defaults are 30 minutes idle expiry, 12 hours absolute expiry, and 20 login
attempts across the service within 15 minutes. The reverse proxy hides the
client address, so v1 does not claim a per-client limit. Admin login
requires the configured `Origin` and `Host`. A successful response sets an
opaque host-only secure cookie and returns a session-bound CSRF token. Clients
send the token in `X-CSRF-Token` with every mutation. Forwarded headers are
rejected; a TLS reverse proxy must preserve the original Host and strip
untrusted forwarding headers. The production admin process binds to loopback.

The private HTML sign-in form shares the same rate limit and session cookie as
the JSON login route. The authenticated Reading desk can inspect bounded
editorial metadata and source lineage. It cannot publish or suppress stories.
The operator still needs to choose the admin origin, certificate and renewal
path, login thresholds, and the private procedure for creating and replacing
the hash and key (ADR 0006).
