# Admin story inspection: Reading desk decision

Status: approved visual direction for the story inspection screen only. The operator called it the “reading deck.” This does not approve an admin overview dashboard.

## Purpose

Let the operator find a candidate story, inspect its source evidence and current public version, and make an exceptional publication decision without treating every withheld item as a required task.

## Layout

- Desktop: bounded story inventory on the left, editorial reading and provenance in the centre, validation and decision rail on the right.
- Narrow screens: one column. Inventory precedes story detail; evidence and current-public comparison precede decision controls. No horizontal overflow at 320px.
- Reuse Harbour Blue tokens, typography, rules, shared controls and states. Public and admin shells remain distinct.

## Information and actions

- Inventory supports linkable search and filters for publication state, topic and source. Show useful freshness and state labels, without a mandatory review-queue count.
- Detail names the candidate revision, current public revision and public URL. Show headline, synthesis, why it matters, material changes, source order/roles/times and exact article → content version → source URL lineage. Keep private raw bodies, prompts, jobs and errors out of the default view.
- Decision rail shows gate results, input fingerprint, validation time and unresolved conflicts. A publish confirmation names the candidate and the revision it will replace. Suppression requires a reason. No state-changing GET.
- Ready, stale, pending, success, partial failure, failure and empty states explain the next action. A failed successor leaves the current public revision live; success links to the public story.
- Ordinary HTML navigation and forms work without HTMX where practical. Maintain visible focus, keyboard order, accessible status text and 44px touch targets.

## Implementation gates

Build the private read model first. Do not expose publish or suppress routes until admin authentication, server-side sessions, CSRF/Origin/host checks and a database-enforced transition/audit boundary pass security review. Bind the actor to the authenticated session; the current trusted backend writer cannot be used directly by admin web.

The separate admin overview/dashboard and routine review queue are outside this approval. Story images follow the separately approved direct-evidence, cited, reuse-cleared local-derivative policy; a story without an eligible image remains text-only.
