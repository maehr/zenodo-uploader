# Contributing

Contributions are welcome — bug reports, fixes, new metadata mappings, documentation,
and reuse examples. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md) in all
project spaces.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.13:

```bash
git clone https://github.com/maehr/zenodo-uploader.git
cd zenodo-uploader
uv sync
```

For anything that talks to Zenodo, copy [`.env.example`](.env.example) to `.env` and
fill in your tokens. Test against <https://sandbox.zenodo.org/> (a separate account and
token) before touching production.

## Before opening a pull request

Run the full local check suite — CI runs the same steps and enforces 100% coverage:

```bash
uv run pytest            # tests + doctests, 100% coverage enforced
uv run ruff check .      # lint
uv run ruff format .     # format
uv run ty check          # type check
uv run prek run --all-files
```

## Pull request process

1. Open an issue first for behavior changes, new tooling, or larger features.
2. Keep pull requests focused and describe the user need they address.
3. Update affected docs together (`README.md`, `examples/`, `AGENTS.md`).
4. Commit with [Conventional Commits](https://www.conventionalcommits.org/) — use
   `uv run cz commit`. The changelog is generated with [git-cliff](https://git-cliff.org/).
5. Make sure the check suite above passes, then request review.

## Releasing

Only a maintainer runs these steps. Do not run them to try them out.

**Caution: a release to PyPI and to the MCP Registry is public and permanent. Check the version first.**

1. Bump the version and write the tag.

   ```bash
   uv run cz bump
   ```

2. Set the same version in `server.json`, in both `version` and `packages[0].version`.
3. Commit that change, then push the branch and the tag.

   ```bash
   git push --follow-tags
   ```

   The `Release` workflow builds the distributions, runs the full gate, and waits
   for approval on the `pypi` environment before it publishes.

4. Publish the server entry to the MCP Registry.

   ```bash
   mcp-publisher login github   # the namespace is io.github.maehr/*
   mcp-publisher publish        # reads server.json
   ```

   The registry verifies ownership by looking for `<!-- mcp-name: io.github.maehr/zenodo-uploader -->`
   in the published README. CI checks that the marker is present.

5. Write the release notes on GitHub with `uv run git-cliff`.

## Reporting security issues

Please do **not** open a public issue for vulnerabilities. See [SECURITY.md](SECURITY.md).
