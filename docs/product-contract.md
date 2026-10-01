# Primary Signal — v1 Product Contract

Status: proposed M0 baseline
Date: 2026-10-01

This document fixes the product rules that implementation and evaluation use.
Architecture and delivery sequencing remain in [PLAN.md](PLAN.md).

## Audience and outcome

Primary Signal serves two v1 users:

- **Reader:** a security practitioner or technically informed decision-maker
  who follows cyber-security developments but cannot read every source. Coverage
  is international, with UK relevance available as a lens rather than a limit.
- **Operator:** the single owner/operator who tunes sources and policy, checks
  system health, and may correct exceptional results. Routine publication does
  not depend on regular editorial review.

The reader should be able to identify the day's material developments in under
five minutes, understand why each matters, and reach the strongest original
evidence within two interactions.

V1 does not try to serve complete beginners, automate incident response, or
replace specialist threat intelligence. It does not personalise coverage for
individual readers.

## Terms

- **Source:** a configured publisher or issuing organisation.
- **Feed entry:** one item observed in RSS or Atom. It is evidence of discovery,
  not the identity of an article.
- **Article:** one source document at a stable canonical identity.
- **Content version:** an immutable retrieval and extraction of an article.
- **Story:** one real-world development, represented by a conservative cluster
  of articles.
- **Synthesis:** Primary Signal's concise account of a story, supported by the
  source trail. It is not presented as a quotation or original reporting.
- **Evidence:** a stored source, passage, identifier, or public artefact that
  supports a specific assertion.
- **Signal:** a defined public marker asserted about a story or article from
  qualifying evidence.
- **Topic:** the subject area a story is chiefly about.
- **Story type:** the editorial form or nature of the development.
- **Lens:** an independent relevance dimension used for filtering or ranking.
- **Tag/entity:** a named CVE, organisation, product, actor, technology, or
  sector. A tag is not a topic by default.
- **Primary source:** material issued by an organisation or person directly
  responsible for, affected by, or reporting original work on the matter.
- **Authoritative source:** a primary source competent to establish the claim,
  such as the affected organisation for an incident or the responsible vendor
  or public authority for an advisory.
- **Independent source:** a source that does not merely syndicate, quote, or
  reproduce another item and is not under the same editorial ownership.
- **Material update:** a change to facts, impact, scope, remediation, or
  confidence that could change a reader's understanding or action.

## Publication contract

The unit of publication is the story. Articles and assertions may be ready at
different times, but a story publishes only when all required parts are valid.

### Story states

| State | Meaning | Permitted next states |
| --- | --- | --- |
| `draft` | Incomplete, held, or failed validation. Never public and does not imply that action is required. | `validated`, `suppressed` |
| `validated` | All mechanical publication gates pass against a fixed input fingerprint. | `published`, `draft`, `suppressed` |
| `published` | Present in public views. | `suppressed`, `superseded` |
| `suppressed` | Excluded by policy, expiry, safety rule, or operator decision, with a reason and actor recorded. | `draft` |
| `superseded` | Retained for history but replaced by another published story. Terminal. | none |

Every transition records its time, actor (`system` or editor), reason, and
input/version identifiers. There is no hard deletion through the editorial
workflow.

- `draft -> validated -> published` is the default automatic path when every
  gate passes. No operator approval is required.
- Failed or uncertain revisions remain unpublished. They may retry when inputs
  or policy change, but they do not create mandatory review work.
- New inputs invalidate `validated`. New inputs for a published story create a
  draft successor revision. The published revision remains live while its
  replacement is prepared unless new evidence makes it unsafe or materially
  misleading; in that case the published revision is suppressed and the
  successor remains in `draft`.
- A safety rule automatically suppresses content found to expose secrets,
  personal data, malicious markup, or unsupported high-consequence claims. It
  records a high-severity event but does not wait for acknowledgement.
- Drafts that become stale, are replaced by newer inputs, or repeatedly fail a
  permanent rule move automatically to `suppressed` with a reason.
- Merge and split operations create membership history. A replaced public story
  becomes `superseded` only after its replacement is published.
- Manual holds, corrections, memberships, taxonomy and signal decisions take
  precedence over later automated runs until the editor clears the override.
- Published dated briefings are immutable snapshots. Corrections are appended
  and link to the current story; the original briefing is not silently rebuilt.

Diagnostics are grouped by source, stage, and failure reason. The admin surface
does not present every withheld story or proposed signal as an editorial task.
Transient failures retry automatically; permanent failures and stale proposals
expire according to policy. The operator may inspect or override them, but no
review queue is required to keep publication running.

### Gates for automatic publication

A routine story may publish automatically only when:

1. it has at least one successfully retrieved, eligible source and a visible
   source trail;
2. its representative claims and synthesis are supported by stored evidence;
3. generated output passes its schema, attribution, length, and unsafe-content
   checks against the current input fingerprint;
4. every automated story membership is above the auto-merge threshold and no
   manual decision conflicts with it; uncertain associations remain separate;
5. primary topic, story type, headline, synthesis, and why-it-matters text are
   present and valid;
6. source times and identity are internally consistent;
7. material conflicts are clearly attributed in the synthesis; otherwise the
   conflicting claim, or the story when necessary, is withheld;
8. every displayed signal has passed the evidence rule below; an uncertain
   optional signal is omitted rather than blocking an otherwise valid story;
9. no source, story, or topic is held or suppressed; and
10. no sensitive-data or rendering safety check fails.

Failure quietly withholds the affected revision and records a specific reason.
It does not create a required editorial task, and a model's confidence score
never overrides a failed gate.

Low-confidence clusters, disputed evidence, unsupported material allegations,
and invalid generated output do not create required editorial work. The system
withholds the affected story or omits the affected signal while eligible work
continues through the pipeline. Daily briefings use the stories that pass; they
do not wait for every candidate.

The editor may inspect exceptions, correct a result, or make a scoped override,
but that is optional tuning rather than the normal route to publication. The
admin surface groups recurring failures and their likely causes instead of
building an ever-growing review queue. Overrides apply only to the current
version unless explicitly recorded as a durable rule.

## Taxonomy contract

Each story has exactly one primary topic, zero to two secondary topics, and
exactly one story type. Classification follows the development itself, not the
section name used by a source.

### Primary topics

- **Vulnerabilities & Exploitation:** a weakness, exposure, exploit chain, or
  exploitation of a specific weakness is central.
- **Threat Activity & Incidents:** malicious activity, campaigns, breaches, or
  operational impact is central.
- **Security Engineering:** defensive design, practice, standards,
  implementation, or architecture is central.
- **Policy & Strategy:** law, regulation, governance, national strategy,
  sanctions, markets, or institutional decisions are central.
- **Research & Tools:** original research, datasets, methods, or released tools
  are central and no more specific topic above is a better fit.

When several apply, choose the topic that best describes what materially
changed. Secondary topics are added only when they would help a reader find the
story; they are not used to hedge an uncertain primary choice.

### Story types

Use one of: **News**, **Research**, **Advisory**, **Incident**, **Analysis**,
**Opinion**, or **Tool Release**. Type describes the underlying development,
not every article in the cluster. Mixed clusters take the type of the strongest
primary evidence. Opinion cannot be converted into fact by corroboration count.

Technical depth, strategic significance, and UK relevance are independent
lenses with stored reasons and policy-versioned scores. UK relevance requires
a direct UK entity, affected population, legal effect, government action, or
material operational consequence; a UK publisher alone is insufficient.

Tags and entities use canonical records plus aliases. CVEs are normalized to
their official identifier. Vendor, product, actor, and sector labels remain
separate entity types. AI Security, Cloud & Identity, and Supply Chain begin as
curated tags and become topics only after a reviewed volume and navigation
case. `High Signal` is ranking, not a badge. `Unverified` is not public in v1.

## Signal evidence rules

Signals are assertions, not decorative tags. Each stores its definition,
scope, qualifying evidence, supporting span or artefact, observed time,
decision route, producing rule/run, and override history. An LLM may propose a
signal and locate candidate support; it cannot be the evidence.

In the table, **automatic** means a deterministic rule can validate and publish
the signal without operator action. Anything outside that route is omitted by
default. The story may still publish when its synthesis remains accurate
without the signal. Proposals are bounded diagnostic data and expire
automatically; the operator may inspect or override them.

| Signal | Minimum qualifying evidence | Automatic route | Optional exception route |
| --- | --- | --- | --- |
| **Primary Source** | The item is issued by the affected/responsible party or presents the author's original research or artefact. | Curated source identity and document ownership match; provenance is intact. | Omit when authorship, ownership, or originality is ambiguous; an editor may confirm it. |
| **Official Advisory** | A public advisory from the responsible vendor, government body, regulator, standards body, or coordinated disclosure authority. | Curated official issuer plus an advisory identifier or advisory document type. | Omit for mirrors, reposts, informal posts, or uncertain issuer scope; an editor may confirm it. |
| **Active Exploitation** | An authoritative source explicitly states exploitation has been observed in the wild, and identifies the affected issue or product. | Exact assertion and identifier from a curated government authority, affected vendor, or named original incident researcher. | Omit anonymous, inferred, secondary-only, or conflicting claims; an editor may approve cited evidence. |
| **Exploit Available** | A publicly reachable working exploit or proof of concept, tied to the same issue by identifier and affected version. | An authoritative advisory explicitly links the artefact, or an allowlisted public artefact repository maps it to the exact identifier and affected version; identity and reachability checks pass. Do not expose a direct exploit link by default. | Omit by default; an editor may confirm identity, capability, and safe presentation. |
| **Actionable** | A competent source gives a concrete mitigation, fixed version, configuration change, detection method, or containment step for the stated scope. | Structured remediation from an official advisory, with product and affected/fixed scope captured. | Omit generic, destructive, disputed, or synthesized advice; an editor may approve a scoped exception. |
| **Confirmed Incident** | The affected organisation or competent public authority confirms that an incident occurred and its wording supports the displayed scope. | Exact confirmation from a curated official channel; the label cannot exceed the confirmed facts. | Omit attributed, unnamed, inferred, or disputed claims; an editor may approve cited evidence. |
| **Developing** | A material fact remains unresolved and there is evidence of an active investigation, changing scope, or credible conflict. | A current authoritative source explicitly says investigation or impact assessment is continuing; expires after 72 hours without a material update. | Omit or expire it when evidence is indirect or conflicting; an editor may retain it with a reason. |
| **Widely Reported** | At least three eligible, independent sources across at least two ownership groups cover the same development within 48 hours. | Deterministic source lineage and clustering meet the rule; syndication and rewrites count once. | Omit when independence or cluster identity is uncertain; an editor may correct source lineage. |
| **Deep Read** | Substantial original reporting, research, or analysis that adds durable technical or strategic understanding beyond the event summary. | Eligible original work passes a corpus-calibrated depth rule based on substantive evidence, method, and added understanding. | Omit by default; an editor may apply it as a curated recommendation. |

Active Exploitation, Exploit Available, and Confirmed Incident are
high-consequence signals. They may not appear publicly from model output or
corroboration count alone. Removing or narrowing any published signal preserves
the old assertion and records the correction.

An unsupported high-consequence signal is omitted automatically. If the same
claim is material to the headline or synthesis, regenerate without it or
withhold the story; do not create a required review item.

## Reference corpus

The v1 evaluation corpus will contain about 150 public articles grouped into
25–40 real stories. It is a versioned test fixture, not a hand-picked showcase.

### Selection method

1. Choose a fixed, recent 8–12 week collection window before model tuning.
2. Build a candidate pool from the source categories below, then select stories
   using a recorded random seed within explicit strata.
3. Cover at least five stories in each primary topic and include every story
   type that occurs in the window. Cap any one publisher at 15% of articles.
4. Deliberately include difficult cases: near-duplicate syndication, evolving
   incidents, one story spanning several days, similar but distinct CVEs,
   corrections, conflicting accounts, opinion near news, and stories that
   should not merge.
5. Aim for 3–6 articles per multi-source story while retaining at least five
   single-source stories. Do not manufacture weak cluster members to hit a
   count.
6. Two passes label expected story membership, representative/primary source,
   primary and secondary topics, story type, entities, signals with evidence,
   UK relevance, and material updates. Disagreements are recorded and resolved
   by the editor; ambiguous cases remain marked as such.
7. Freeze a development set and a blind acceptance set before thresholds and
   prompts are tuned. Do not move failed examples out of the acceptance set.
8. Version the manifest and labels. Add later regression cases without changing
   the expected result of an existing item silently.

The repository may contain public URLs, normalized metadata, content hashes,
labels, short evidence spans where lawful, and synthetic edge cases. It must
not contain copied full article bodies, access tokens, real operational logs,
private feeds, local paths, or deployment details. Retrieved bodies remain in
ignored local storage and can be rebuilt from the manifest where access and
terms permit. Tests that need stable full text use clearly synthetic fixtures.

### Initial source categories

The candidate pool should draw from:

- national cyber-security authorities and vulnerability catalogues;
- vendor security advisories and product incident notices;
- affected-organisation statements and regulator disclosures;
- independent vulnerability and malware research teams;
- established cyber-security newsrooms;
- general newsrooms with dedicated security reporting;
- standards bodies, policy institutions, courts, and government publications;
- open-source project advisories and release notes;
- original technical blogs from named practitioners; and
- public research papers, datasets, and tool releases.

Aggregators, automated rewrites, anonymous social posts, and scraped mirrors may
be retained as negative or provenance test cases, but they are not sufficient
evidence for publication on their own.

## M0 acceptance checks

M0 is complete when:

- the corpus manifest and labelling guide implement this contract;
- every expected public signal cites evidence and a decision route;
- corpus labels can express uncertainty and explicit non-merges;
- automated evaluation reports cluster precision separately from recall and
  treats false joins as the more serious error;
- taxonomy and publication fixtures cover every rule above; and
- changes to these rules require a recorded, versioned decision rather than a
  silent prompt or threshold change.
