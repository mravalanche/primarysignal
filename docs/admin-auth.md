# Admin authentication backend

The admin application is a separate web process. In development and tests,
an unconfigured admin app serves only liveness routes. Production startup
requires an HTTPS admin origin, a local Argon2id PHC password hash, and a
32-byte or longer session key supplied as hexadecimal. It also requires a
database login with only `primary_signal_cap_admin_session` membership.

The deployment-owned settings are `PRIMARY_SIGNAL_ADMIN_ORIGIN`,
`PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC`, and
`PRIMARY_SIGNAL_ADMIN_SESSION_KEY_HEX`. The usual
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`
identify the separate session-only login. Do not place their values in the
repository, shell history, or logs. The password hash and session key are
independent; replacing either invalidates existing sessions.

The defaults are 30 minutes idle expiry, 12 hours absolute expiry, five login
attempts per source and 20 across the service within 15 minutes. Admin login
requires the configured `Origin` and `Host`. A successful response sets an
opaque host-only secure cookie and returns a session-bound CSRF token. Clients
send the token in `X-CSRF-Token` with every mutation. Forwarded headers are
rejected; a TLS reverse proxy must preserve the original Host and strip
untrusted forwarding headers. The production admin process binds to loopback.

This backend adds no visible login page or editorial write route. Before an
admin interface is exposed, the operator still needs to choose the admin
origin, certificate and renewal path, login thresholds, and the private
procedure for creating and replacing the hash and key (ADR 0006).
