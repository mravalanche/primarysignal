# Theme studies

The [parent design page](../../DESIGN.md) compares all three directions. Open
the [hosted component showcase](https://mravalanche.github.io/primarysignal/design/component-showcase.html)
or inspect its [HTML source](../../design/component-showcase.html). Each page
below records the light and dark study, core tokens and component rules.

| Theme | Status | Character |
| --- | --- | --- |
| [Harbour Blue](harbour-blue.md) | Selected and live | Calm technical publication |
| [Ember Ledger](ember-ledger.md) | Exploratory | Warm newspaper character |
| [Field Notes](field-notes.md) | Exploratory | Research-journal restraint |

The [design system](../design-system.md) remains the implementation reference
for Harbour Blue. Exploratory pages describe options; they are not additional
runtime themes.

## Regenerate the images

The images are browser captures of the standalone HTML, not separate mockups.
With the project's pinned Node version, run `npm ci`,
`npx playwright install chromium`, then `npm run capture:design`. The capture
script renders all six theme and mode combinations at 1440 px and crops the
component section for the comparison in the parent page. Review the generated
images before committing them.
