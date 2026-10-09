# October 2026 reference-corpus selection

This is the collection plan for the first real M0 corpus. The current
`synthetic-v1.json` fixture tests the format and evaluator only. No real article
or story labels have been accepted yet.

## Fixed window and sample

- Publication window: 20 July through 4 October 2026, inclusive (11 complete
  weeks). Record the article's original publication time, not its discovery
  time.
- Sampling seed: `20261009`. Record the candidate-pool revision and selection
  script before drawing articles. Keep the raw candidate list and any exclusion
  reason so selection can be reproduced.
- Target: about 150 public article references across 33 real stories. Keep at
  least five genuinely single-source stories. Aim for three to six articles in
  each multi-source story without adding weak matches to fill a quota.
- Publisher cap: at most 22 of 150 articles from one publisher. Track common
  ownership and syndication separately; differently named outlets do not
  automatically count as independent corroboration.

| Primary topic | Story target | Article target |
| --- | ---: | ---: |
| Vulnerabilities & Exploitation | 8 | 40 |
| Threat Activity & Incidents | 8 | 40 |
| Security Engineering | 6 | 28 |
| Policy & Strategy | 6 | 24 |
| Research & Tools | 5 | 18 |
| **Total** | **33** | **150** |

Assign each candidate one primary source category for sampling, even if it
could fit several. These targets balance original evidence and reporting; they
are not publication weights.

| Source category | Article target |
| --- | ---: |
| National authorities and vulnerability catalogues | 18 |
| Vendor advisories and notices | 27 |
| Affected-organisation statements and regulator disclosures | 12 |
| Independent research | 24 |
| Cyber newsrooms | 28 |
| General newsroom security coverage | 12 |
| Standards, policy, courts, and government | 11 |
| Open-source advisories and releases | 8 |
| Named practitioner blogs | 5 |
| Papers, datasets, and tools | 5 |
| **Total** | **150** |

## Selection and acceptance

Draw within topic, period, and source-category strata using the recorded seed.
Keep an explicit oversample of difficult cases: syndication, evolving and
multi-day incidents, similar but distinct CVEs, corrections, conflicting
accounts, and opinion close to news. Include at least two examples of each
where the window genuinely provides them; cases may overlap. Record gaps rather
than inventing examples.

Reserve 10 whole stories for blind acceptance, spread across all five topics
and the difficult cases. Never split one story's articles across development
and acceptance. Freeze the split before tuning prompts or clustering thresholds.
Perform two independent label passes for membership, source primacy, topics,
entities, UK relevance, material updates, and evidence-backed signals. Record
disagreements and editorial adjudication. Keep ambiguous pairs marked as
uncertain and explicit near-miss pairs as non-merges.

Only public URLs and metadata, labels, short lawful evidence references, and
synthetic test content belong in this repository. Do not commit article bodies,
private feeds, access details, local paths, or operational history. The
selection is complete when the versioned manifest passes validation and the
accepted labels make the clustering precision gate measurable.
