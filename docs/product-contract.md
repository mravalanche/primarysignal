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
- **Editor:** the single owner/operator who curates sources, resolves uncertain
  clusters and evidence, corrects publication, and runs the service.

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
| `draft` | Incomplete, held, failed validation, or awaiting review. Never public. | `validated`, `suppressed` |
| `validated` | All mechanical publication gates pass against a fixed input fingerprint. | `published`, `draft`, `suppressed` |
| `published` | Present in public views. | `suppressed`, `superseded` |
| `suppressed` | Intentionally excluded, with a reason and actor recorded. | `draft` |
| `superseded` | Retained for history but replaced by another published story. Terminal. | none |

Every transition records its time, actor (`system` or editor), reason, and
input/version identifiers. There is no hard deletion through the editorial
workflow.

- `draft -> validated` is automatic only when every validation gate passes.
- `validated -> published` is automatic for routine stories unless an
  editorial gate below applies. The editor may also publish explicitly.
- New inputs invalidate `validated`. New inputs for a published story create a
  draft successor revision. The published revision remains live while its
  replacement is prepared unless new evidence makes it unsafe or materially
  misleading; in that case the published revision is suppressed and the
  successor remains in `draft`.
- `published -> suppressed` is an editorial action, except for a narrow safety
  rule that withdraws content found to expose secrets, personal data, malicious
  markup, or unsupported high-consequence claims. Automatic withdrawal creates
  an urgent review item.
- Merge and split operations create membership history. A replaced public story
  becomes `superseded` only after its replacement is published.
- Manual holds, corrections, memberships, taxonomy and signal decisions take
  precedence over later automated runs until the editor clears the override.
- Published dated briefings are immutable snapshots. Corrections are appended
  and link to the current story; the original briefing is not silently rebuilt.

### Gates for automatic publication

A routine story may publish automatically only when:

1. it has at least one successfully retrieved, eligible source and a visible
   source trail;
2. its representative claims and synthesis are supported by stored evidence;
3. generated output passes its schema, attribution, length, and unsafe-content
   checks against the current input fingerprint;
4. cluster confidence is above the auto-merge threshold and no manual decision
   conflicts with it;
5. primary topic, story type, headline, synthesis, and why-it-matters text are
   present and valid;
6. source times and identity are internally consistent;
7. no material conflict between credible sources is unresolved;
8. every public signal has passed the evidence rule below;
9. no source, story, or topic is held or suppressed; and
10. no sensitive-data or rendering safety check fails.

Failure leaves the story in `draft` with a specific, visible reason. A model's
confidence score never overrides a failed gate.

Editorial approval is required for a low-confidence or disputed cluster, an
exception to a source/evidence rule, a material allegation about a person or
organisation that lacks authoritative confirmation, or any manual override of
validation. Approval applies to the current version only unless explicitly
recorded as a durable rule.

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
the signal. Anything outside that exact route remains proposed until an editor
approves or rejects it.

| Signal | Minimum qualifying evidence | Automatic route | Editorial gate |
| --- | --- | --- | --- |
| **Primary Source** | The item is issued by the affected/responsible party or presents the author's original research or artefact. | Curated source identity and document ownership match; provenance is intact. | Required when authorship, ownership, or originality is ambiguous. |
| **Official Advisory** | A public advisory from the responsible vendor, government body, regulator, standards body, or coordinated disclosure authority. | Curated official issuer plus an advisory identifier or advisory document type. | Required for mirrors, reposts, informal posts, or uncertain issuer scope. |
| **Active Exploitation** | An authoritative source explicitly states exploitation has been observed in the wild, and identifies the affected issue or product. | Exact assertion and identifier from a curated government authority, affected vendor, or named original incident researcher. | Required for anonymous claims, inference from scanning, secondary-only reports, or conflicting scope. |
| **Exploit Available** | A publicly reachable working exploit or proof of concept, tied to the same issue by identifier and affected version. | None in v1. Automation may propose and verify reachability only. | Always required; the editor checks identity, capability, and whether linking would create avoidable harm. |
| **Actionable** | A competent source gives a concrete mitigation, fixed version, configuration change, detection method, or containment step for the stated scope. | Structured remediation from an official advisory, with product and affected/fixed scope captured. | Required for generic advice, destructive steps, disputed mitigations, or synthesis across sources. |
| **Confirmed Incident** | The affected organisation or competent public authority confirms that an incident occurred and its wording supports the displayed scope. | Exact confirmation from a curated official channel; the label cannot exceed the confirmed facts. | Required for attributed secondary reporting, unnamed sources, victim inference, or disputed impact. |
| **Developing** | A material fact remains unresolved and there is evidence of an active investigation, changing scope, or credible conflict. | A current authoritative source explicitly says investigation or impact assessment is continuing; expires after 72 hours without a material update. | Required to add, retain, or remove it when evidence is indirect or sources conflict. |
| **Widely Reported** | At least three eligible, independent sources across at least two ownership groups cover the same development within 48 hours. | Deterministic source lineage and clustering meet the rule; syndication and rewrites count once. | Required when independence or cluster identity is uncertain. |
| **Deep Read** | Substantial original reporting, research, or analysis that adds durable technical or strategic understanding beyond the event summary. | None in v1. | Always required because depth and lasting value are editorial judgements. |

Active Exploitation, Exploit Available, and Confirmed Incident are
high-consequence signals. They may not appear publicly from model output or
corroboration count alone. Removing or narrowing any published signal preserves
the old assertion and records the correction.

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
