# Repository rulesets

These files are bootstrap rulesets for the protected `develop` and `main`
branches. They are stored here for review and recovery; GitHub does not apply
them automatically.

Import each JSON file from **Settings → Rules → Rulesets → New ruleset → Import
a ruleset** after both target branches exist. Check the imported settings, then
save each ruleset as active.

The initial files deliberately require no approving review because the
repository has one maintainer. They still require a pull request and resolved
review conversations. When a second trusted maintainer is available, change
both rulesets to require one approval, dismiss stale approvals, require approval
of the latest push, and require Code Owner review where useful.

Required status checks are also omitted at bootstrap. Add them only after the
corresponding workflow jobs have run successfully at least once:

- `Quality`
- `Secret scan`
- `Dependency audit`
- `release-source` on `main` only

When adding status checks, require the branch to be up to date before merging.
The `release-source` check must accept only `develop` as the head branch of a
pull request into `main`.
