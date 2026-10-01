# ADR 0008: Server-rendered UI and constrained component layer

- Status: Accepted
- Date: 2026-10-01

## Decision

Render the public and administration interfaces with FastAPI and Jinja. Use
HTMX only where an interaction benefits from partial page updates. Build styles
with Tailwind CSS 4 and a selected subset of DaisyUI 5 components.

DaisyUI is an internal primitive layer. Shared Jinja components own its class
names; publication templates should use Primary Signal components rather than
assemble DaisyUI markup repeatedly. Editorial structures such as story rows,
source trails, tags and named evidence signals remain product-specific.

Node is required for asset builds and checks, not in production. Dependencies
are exactly pinned in `package.json` and resolved in `package-lock.json`.
Runtime assets are compiled and self-hosted; production pages do not use a CDN.

Public and administration surfaces have separate template and asset roots.
They share strict, autoescaping Jinja primitives and foundational design tokens.
Custom light and dark themes disable DaisyUI depth and noise effects and use
restrained radii.

## Consequences

- Pages remain useful as ordinary links and forms without client-side state.
- Repeated controls and states have one implementation and can be replaced
  without rewriting every page.
- A development-only component catalogue exercises normal, empty, error and
  difficult-content states.
- The frontend toolchain adds a locked build step, but not another production
  service or application runtime.
- DaisyUI defaults must not become the product's visual language. New component
  families are added deliberately, not enabled wholesale.

Bookmarks and personalisation are outside the current UI scope. Public signal
labels use the evidence-gated signal kinds in the product contract; the UI does
not invent a composite confidence or strength score.
