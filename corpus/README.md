# Reference corpus

This directory holds versioned labels and public metadata for evaluating story
clustering and publication rules. `synthetic-v1.json` is a small, invented fixture
that exercises the format; it is not the v1 acceptance corpus.

The fixed first collection plan is in [selection-2026-10.md](selection-2026-10.md).
The small [candidate pilot](candidate-pilot-2026-10.json) records verified public
URLs and dates from several source categories. It checks source availability;
it is neither a sampled candidate pool nor a labelled acceptance set.
The [second candidate batch](candidate-batch2-2026-10.json) adds another 15
verified public references, including three accounts of the same F5 advisory.
These remain availability candidates, not selected or labelled corpus articles.

## Labelling guide

Follow the selection method in [the product contract](../docs/product-contract.md#reference-corpus).
Record the collection window and random seed before selecting examples. Keep a
development set and a blind acceptance set fixed before tuning thresholds or
prompts. Never move a failed acceptance example into development. Add a new
version when a settled label changes, and record why.

For each public article, record its canonical public URL, publisher, ownership
group, source category, publication time, and title. Do not store article bodies,
private feeds, operational logs, or local paths. Label each story twice, then
resolve disagreements with a recorded editorial note. Keep ambiguous pairs in
`uncertain_pairs`; do not force them into a merge or a non-merge. Record explicit
`non_merges` for similar developments that must stay separate, especially
distinct CVEs, corrections, and evolving incidents.

Each story label names its representative article, primary source, and why that
source is primary. It also records a primary topic, up to two secondary topics,
story type, typed entities, UK relevance, material updates, and uncertainty.
A public signal must name a supporting
article, a short evidence reference or public artefact, its scope, and either
the deterministic or editor decision route. An uncertain proposed signal is
recorded with `uncertain: true` and is not an expected public signal.

`python scripts/evaluate_corpus.py corpus/synthetic-v1.json predictions.json`
checks both files and prints pairwise cluster precision and recall by split.
False joins, including explicit non-merges, are listed individually. Precision
is the priority: merging distinct stories can publish misleading claims.

The real acceptance set must be held out from tuning. This repository's
synthetic blind split only tests the evaluator; it does not measure product
quality. The real corpus still needs the contract's roughly 150 public articles
and 25–40 stories, source balance, difficult cases, and editorial adjudication.
