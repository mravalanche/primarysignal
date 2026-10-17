# Development container image

After the Quality, Secret scan and Dependency audit jobs pass on a push to
`develop`, CI builds the runtime image and publishes it to
`ghcr.io/mravalanche/primarysignal`. The tag is `dev-<full commit SHA>`.
The Publish development image job records the immutable `sha256` image
reference in its GitHub Actions run summary. Use that full digest when
running a disposable stack on another Docker host; a tag alone can be moved.

The image contains the installed application, built public and admin assets,
and Alembic migration files. The same image can run the web, migration and
synthetic seed entrypoints. The current CI runner publishes a Linux amd64
image; check the target Docker host's architecture before pulling it.

Image publication uses the repository's built-in `GITHUB_TOKEN` with package
write permission only for the publishing job. The repository owner controls
GHCR package visibility. If the package is private, the operator must
authenticate Docker to GHCR on the target host using their own credentials.
Do not put registry credentials in Compose files or this repository.

The image is a development artifact, not a production release. The disposable
publication bundle and operator-owned local configuration are documented in
`docs/disposable-publication.md` once that stack is ready.
