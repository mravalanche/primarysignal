# Primary Signal project guidance

## Naming

- Product name: **Primary Signal**.
- Repository and workspace name: `primarysignal`.
- Python package name: `primary_signal`.

## Public repository safety

This repository is public. Treat every committed file, fixture, screenshot,
log, test output, issue reference, and example as public information.

Do not commit:

- secrets, credentials, tokens, hashes derived from real credentials, or
  private keys;
- real LAN addresses, internal hostnames, tunnel IDs, account IDs, user names,
  filesystem paths, hardware identifiers, or private service URLs;
- production configuration, live firewall rules, deployment inventories, or
  details that reveal the actual local security posture;
- real logs, database contents, article-processing history, or admin captures;
- personal information or details of the operator's home or local environment.

Use obvious placeholders such as `public.example`, `admin.example.test`,
`192.0.2.10`, and `REPLACE_ME`. Keep deployable local configuration in ignored
files and provide sanitized `.example` templates.

General architecture, threat models, intended controls, and security tests are
appropriate for the public repository. Describe the secure target design, not
unnecessary details of a live installation or an unpatched weakness. If a
document could make the real deployment easier to target, stop and ask before
committing it.

Before each commit, inspect both the diff and newly added files for sensitive
or identifying information. Add automated secret scanning early.

## Writing style

Write like a careful human editor:

- Be concise, direct, and specific.
- Prefer short sentences and ordinary words.
- Remove repetition, throat-clearing, and inflated claims.
- Avoid stock AI language such as “seamless”, “robust”, “cutting-edge”,
  “revolutionise”, “leverage”, and “delve”.
- Do not turn every explanation into a long list or repeat the conclusion.
- State uncertainty plainly. Do not manufacture confidence or marketing copy.
- UI copy should help the reader act; it should not narrate the interface.

Documentation can be detailed where the detail is useful, but should still be
edited down before it is committed.

## Product and visual character

Primary Signal is an editorial cyber-security publication, not an AI product
showcase. AI is an implementation detail and should not dominate the language
or visual identity.

Avoid generic generated-product patterns:

- gradient-heavy hero sections and oversized slogans;
- glassmorphism, excessive glow, neon “hacker” styling, and Matrix imagery;
- interchangeable SaaS dashboards, giant metric tiles, and three-card feature
  grids used as filler;
- decorative chatbots, sparkles, brain/circuit imagery, and “AI-powered” badges;
- excessive pills, icons, shadows, animation, and rounded containers;
- placeholder copy, fake statistics, invented testimonials, and stock imagery.

Prefer strong editorial typography, a clear reading rhythm, restrained colour,
thin rules, compact technical metadata, deliberate whitespace, and layouts that
make source provenance easy to understand. Reuse shared components and tokens.
Every visual element should earn its place.

## UI approval

Before implementing a new page, shell, component pattern, or material visual
change, present two or three concrete options with examples and, where useful,
mock-ups. Explain the trade-offs and wait for the operator to choose a direction.
Do not commit a visual direction to the public repository before that approval.

## UI implementation

- Follow [the selected Harbour Blue design system](docs/design-system.md) for
  colour ramps, semantic aliases, typography, spacing, components, and the
  original A4c1 Valley mark in light and dark modes.
- Render the public and administration interfaces with FastAPI and strict,
  autoescaping Jinja templates.
- Use HTMX for focused progressive enhancement. Links, filters, navigation and
  forms must retain an ordinary HTML path where practical.
- Build styles with Tailwind CSS and a constrained DaisyUI component subset.
  DaisyUI is an internal primitive layer, not the product's visual language.
- Keep DaisyUI classes behind shared Jinja components. Story rows, source
  trails, tags, named signals and editorial layouts are Primary Signal
  components.
- Keep public and administration templates and assets separate while sharing
  foundational tokens and accessible primitives.
- Use named, evidence-gated signals and source provenance. Do not invent a
  composite confidence or signal-strength score.
- Bookmarks and personalisation are outside the current product scope.

## Parallel work

Delegate longer-running tests, audits, dependency operations, research, and
independent reviews to subagents when they can run safely in parallel. Keep the
main chat available for discussion and decisions while that work runs. Keep
short or sequential work with the coordinating agent; do not create subagents
merely for ceremony.

## Security-sensitive changes

Authentication, sessions, retrieval, SSRF controls, network boundaries,
database roles, secrets, public routing, backups, and deployment changes need a
focused security review. Tests and documentation must use synthetic data and
reserved example addresses.
