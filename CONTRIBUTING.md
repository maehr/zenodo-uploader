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

## Reporting security issues

Please do **not** open a public issue for vulnerabilities. See [SECURITY.md](SECURITY.md).
