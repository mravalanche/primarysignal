# Bounded three-source ingestion proof

`uv run --locked --offline poe -q live-source-smoke` explicitly enables real
feed and article requests in the verified isolated development topology.
It is not a production deployment or unattended collector. The default
synthetic task continues to make no external requests.

## Fixed source plan

| Class | Feed | Provider reference |
| --- | --- | --- |
| Authority | [CERT-EU advisories](https://cert.europa.eu/publications/security-advisories-rss) | [Official feed announcement](https://cert.europa.eu/blog/read-feed-and-lead-fresh-features-for-our-cyber-fans) |
| Vendor | [Ubuntu security notices](https://ubuntu.com/security/notices/rss.xml) | [Official notices page](https://ubuntu.com/security/notices) |
| Research | [SANS ISC diary](https://isc.sans.edu/rssfeed.xml) | [Official feed directory](https://isc.sans.edu/xml.html) |

The reviewed provider hosts are `cert.europa.eu`, `ubuntu.com` and
`isc.sans.edu`. The DNS fixture forwards only those exact names and the
existing `example.com` prerequisite, retaining A and AAAA answers through the
fixed approved resolver. It does not add subdomains, CNAME targets, providers
or private resolver fallbacks automatically.

## Work and evidence bounds

The task runs the synthetic enforcement, pipeline, managed restart and transit
checks before live ingestion. It then permits at most three durable feed-job
attempts and three selected article-job attempts. DNS, connection and redirect
activity retains the transport's separate finite bounds.

One eligible HTTPS article is selected deterministically per source through
its actual feed entry, article and canonical-URL lineage. All selections are
fixed before article processing. Unselected, unattempted jobs belonging to the
proof are deferred in its disposable database; no continuous scheduler or
worker starts. A provider or selected article failure stops the proof without
substitution, a retry loop, a new provider or a retrieval bypass.

A pass requires a successful fetch, nonempty current immutable content version,
correct source provenance and a genuine full-text search hit for each article.
Search tokens are derived privately from extracted text. A filtered inventory
listing alone is insufficient. Challenge, access-denied and consent pages are
not accepted as articles. Returned article redirect and final hosts must remain within
the reviewed provider host set under the normal bounded transport checks. The
current feed-poll record retains the requested feed URL and outcome, not a
durable final-URL/redirect chain; this proof does not claim that extra feed
evidence.

## Privacy and scope

Only aggregate results leave the fixture. Live article URLs, titles, IDs,
hashes, text, search tokens and detailed processing history stay in private
memory or the disposable volume. They are not test fixtures, log artifacts,
committed verification records or publication content. The runner removes its
owned containers, logs, networks, images, volume and temporary files on every
outcome.

This proof can complete the three-source inventory exit check only after all
three source classes and cleanup pass. Production runtime readiness, unattended
fetching, target deployment controls and retention/backup operation remain
separate work. See [the isolation and transit proof](isolated-retrieval.md).

## Verification checkpoint

On 2026-10-28, one quiet bounded invocation returned failure after three feed
attempts and three selected article attempts. Two source classes and two
full-text search checks passed. Cleanup passed. No retry, replacement article
or provider substitution was made.

This does not complete the three-source exit check. The privacy boundary
removed live article details, processing history and logs with the disposable
storage, so this checkpoint makes no claim about the cause of the incomplete
result. Further investigation needs a separately bounded run under the same
controls, not a success assertion based on partial results.

The implementation passed 1,415 automated tests with no skips, including four
PostgreSQL preflight cases using synthetic data and the actual private proof
program. Those tests verify the tool's bounds and database logic; they do not
substitute for the missing real-source result.
