# zenodo-uploader

[![CI](https://github.com/maehr/zenodo-uploader/actions/workflows/ci.yml/badge.svg)](https://github.com/maehr/zenodo-uploader/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.13-blue.svg)](pyproject.toml)

> Mirror DOIs and upload records to [Zenodo](https://zenodo.org/) from the command line.

**zenodo-uploader** takes a DOI, fetches its metadata from [DataCite](https://api.datacite.org/) (falling back to [Crossref](https://api.crossref.org/)), maps it onto Zenodo deposit metadata, attaches files, and creates (and optionally publishes) the deposition via the [Zenodo REST API](https://developers.zenodo.org/). It reuses the existing DOI by default, so mirrored records keep their canonical identifier instead of getting a new Zenodo DOI. Batch mode mirrors whole DOI lists with a resumable state file.

Any DOI works — see [`examples/`](examples/) for one-off DataCite and Crossref DOIs as well as a full batch pipeline.

## Installation

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.13:

```bash
git clone https://github.com/maehr/zenodo-uploader.git
cd zenodo-uploader
uv sync
```

## Authentication

1. Create a personal access token at <https://zenodo.org/account/settings/applications/tokens/new/> with the scopes **`deposit:write`** and **`deposit:actions`**.
2. For testing, create a *separate* account and token on <https://sandbox.zenodo.org/> (the sandbox is a fully independent instance).
3. Export the tokens (or put them in a `.env` file next to your working directory):

```bash
export ZENODO_TOKEN=...          # zenodo.org
export ZENODO_SANDBOX_TOKEN=...  # sandbox.zenodo.org
```

## Usage

```bash
# Inspect the mapped metadata without touching Zenodo
uv run zenodo-uploader from-doi 10.5555/example-chapter --dry-run

# Create a draft on the sandbox with a file attached
uv run zenodo-uploader from-doi 10.5555/example-chapter \
    --file chapter.pdf --community my-community --sandbox

# Publish on the sandbox
uv run zenodo-uploader from-doi 10.5555/example-chapter \
    --file chapter.pdf --sandbox --publish

# Publish on production (interactive confirmation: published records
# on zenodo.org can never be deleted)
uv run zenodo-uploader from-doi 10.5555/example-chapter --file chapter.pdf --publish

# Check whether a DOI is already mirrored
uv run zenodo-uploader check 10.5555/example-chapter

# Upload raw Zenodo metadata JSON, e.g. a .zenodo.json (no DataCite involved).
# Fields are sent verbatim; omit "doi" to mint a new DOI, include it to keep one.
uv run zenodo-uploader upload --metadata .zenodo.json --file data.csv --sandbox

# Mirror a whole manifest, resumable via the state file
uv run zenodo-uploader batch --manifest manifest.json --state state.json --sandbox
```

### Safety ladder

1. `--dry-run` — print the mapped metadata; no writes at all.
2. default — create a **draft** deposition (drafts can be deleted).
3. `--publish` — publish; on production this asks you to type `PUBLISH` unless `--yes` is given.

### Manifest format

`batch` reads a JSON array; `files` are local paths, everything except `doi` is optional:

```json
[
  {
    "doi": "10.5555/example-chapter",
    "files": ["files/example-chapter.pdf", "files/example-chapter.html"],
    "community": "my-community",
    "description": "<p>Optional HTML description override.</p>",
    "related": [{ "relation": "isPartOf", "identifier": "10.5555/example-book" }]
  }
]
```

The state file records one row per DOI (`draft`, `published`, `exists`, or `error`); rerunning the batch skips finished rows and retries errors.

### Metadata mapping

DOIs are resolved from DataCite first, then Crossref; both registries map onto the same fields:

| Source (DataCite / Crossref)                              | Zenodo                                  |
| --------------------------------------------------------- | --------------------------------------- |
| `titles[0].title` / `title[0]`                            | `title`                                 |
| `creators` / `author` + `editor` (name, ORCID)            | `creators`                              |
| `types.resourceTypeGeneral` / `type`                      | `upload_type` / `publication_type`      |
| `rightsList[].rightsIdentifier` / `license[].URL`         | `license`                               |
| `Issued` date or `publicationYear` / `issued`             | `publication_date` (full date or `YYYY-01-01`) |
| `publisher`                                               | `imprint_publisher` (publications)      |
| `relatedIdentifiers`                                      | `related_identifiers`                   |
| `url` / `resource.primary.URL` (landing page)            | `related_identifiers` (`isIdenticalTo`) |
| DOI                                                       | `doi` (kept — no new DOI is minted)     |

Zenodo requires a non-empty description; when the registry has none, a minimal HTML description with the citation and links is synthesized (override with `--description`).

### Uploading a `.zenodo.json`

`upload` takes any Zenodo deposit metadata JSON, including a
[`.zenodo.json`](https://help.zenodo.org/docs/github/describe-software/zenodo-json/)
file (fields at the top level or wrapped in `{"metadata": {...}}`). Every field is
forwarded to Zenodo verbatim — Zenodo-specific ones such as `version`,
`access_right`, `contributors`, `grants`, and `communities` are preserved. `title`,
`upload_type`, `description`, `publication_date`, and each creator's `name` are
required; a missing one fails with a clear message before any request is sent. Omit
the `doi` field to have Zenodo mint a new DOI, or include it to keep an existing one.
See [`examples/.zenodo.json`](examples/.zenodo.json).

### Community curation

Records submitted with `--community` create an inclusion request on publish; a community curator must accept it (in the community's *Requests* tab) before the record appears in the community.

## Development

```bash
uv run pytest            # tests + doctests, 100% coverage enforced
uv run ruff check .      # lint
uv run ruff format .     # format
uv run ty check          # type check
uv run prek run --all-files
```

See [AGENTS.md](AGENTS.md) for the tooling specification. Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`uv run cz commit`); the changelog is generated with [git-cliff](https://git-cliff.org/).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Please report it privately — see [SECURITY.md](SECURITY.md). Never paste your Zenodo tokens into issues or logs.

## Citation

If you use zenodo-uploader, please cite it using the metadata in [CITATION.cff](CITATION.cff) (GitHub's "Cite this repository" button).

## License

Code: [AGPL-3.0-only](LICENSE).
