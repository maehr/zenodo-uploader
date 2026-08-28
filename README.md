# zenodo-uploader

[![CI](https://github.com/maehr/zenodo-uploader/actions/workflows/ci.yml/badge.svg)](https://github.com/maehr/zenodo-uploader/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.13-blue.svg)](pyproject.toml)

> Mirror DOIs and upload records to [Zenodo](https://zenodo.org/), from an agent or from the command line.

<!-- mcp-name: io.github.maehr/zenodo-uploader -->

**zenodo-uploader** takes a DOI, reads its metadata from [DataCite](https://api.datacite.org/) or [Crossref](https://api.crossref.org/), maps it onto Zenodo deposit metadata, attaches your files, and creates the deposition through the [Zenodo REST API](https://developers.zenodo.org/). The record keeps its original DOI, so a mirror stays citable under one identifier.

The project ships three interfaces over one core:

| Interface       | Use it when                                            |
| --------------- | ------------------------------------------------------ |
| **MCP server**  | An agent does the work. Six tools over stdio.          |
| **Agent Skill** | You use Claude Code and want the procedure as well.    |
| **CLI**         | You work at a terminal, or you mirror a list of DOIs.  |

## Install

Everything runs through [uv](https://docs.astral.sh/uv/). You need Python 3.13 or later.

```bash
uvx --from zenodo-uploader zenodo-uploader --help
```

To work on the code instead, clone the repository:

```bash
git clone https://github.com/maehr/zenodo-uploader.git
cd zenodo-uploader
uv sync
```

## Authenticate

1. Open <https://zenodo.org/account/settings/applications/tokens/new/>.
2. Select the scopes `deposit:write` and `deposit:actions`.
3. Create the token, then copy it.
4. Export the token, or write it into a `.env` file.

```bash
export ZENODO_TOKEN=...          # zenodo.org
export ZENODO_SANDBOX_TOKEN=...  # sandbox.zenodo.org
```

The sandbox is a separate service with separate accounts. To rehearse, register again at <https://sandbox.zenodo.org/>.

**Caution: never commit a token, and never paste one into an issue or a log.**

## Use it from an agent

### As a plugin in Claude Code

One install gives you the skill and the server together:

```
/plugin marketplace add maehr/zenodo-uploader
/plugin install zenodo-uploader@maehr
```

### As a plain MCP server

Add this to your MCP client configuration:

```json
{
  "mcpServers": {
    "zenodo": {
      "command": "uvx",
      "args": ["--from", "zenodo-uploader", "zenodo-mcp"],
      "env": { "ZENODO_SANDBOX_TOKEN": "..." }
    }
  }
}
```

The server offers six tools:

| Tool                  | Writes | Purpose                                               |
| --------------------- | ------ | ----------------------------------------------------- |
| `preview_doi`         | no     | Show the Zenodo metadata that a DOI maps to.          |
| `check_doi`           | no     | Report whether a DOI is already on Zenodo.            |
| `get_deposition`      | no     | Read the state, DOI, title, and link of a deposition. |
| `mirror_doi`          | yes    | Mirror a DOI, with files attached.                    |
| `upload_record`       | yes    | Create a record from metadata that you supply.        |
| `submit_to_community` | yes    | Send an existing draft to a community.                |

Every tool works against the sandbox unless you pass `sandbox: false`.

**Caution: a published record on zenodo.org is permanent.** The server therefore refuses a production publish unless both of these are true:

- you started the server with `ZENODO_ALLOW_PRODUCTION_PUBLISH=1`, and
- the call passes `confirm="PUBLISH"`.

## Use it from the command line

```bash
# Show the mapped metadata. This writes nothing.
uvx --from zenodo-uploader zenodo-uploader from-doi 10.30965/9783657796823 --dry-run

# Create a draft on the sandbox, with a file attached
uvx --from zenodo-uploader zenodo-uploader from-doi 10.30965/9783657796823 \
    --file chapter.pdf --community my-community --sandbox

# Publish on the sandbox
uvx --from zenodo-uploader zenodo-uploader from-doi 10.30965/9783657796823 \
    --file chapter.pdf --sandbox --publish

# Publish on production. The command asks you to type PUBLISH.
uvx --from zenodo-uploader zenodo-uploader from-doi 10.30965/9783657796823 \
    --file chapter.pdf --publish

# Ask whether a DOI is already mirrored
uvx --from zenodo-uploader zenodo-uploader check 10.30965/9783657796823

# Upload Zenodo metadata that you already have, such as a .zenodo.json
uvx --from zenodo-uploader zenodo-uploader upload --metadata .zenodo.json --file data.csv --sandbox

# Mirror a whole list of DOIs. The state file makes the run resumable.
uvx --from zenodo-uploader zenodo-uploader batch --manifest manifest.json --state state.json --sandbox
```

### The safety ladder

Climb one rung at a time.

1. `--dry-run` prints the mapped metadata and writes nothing.
2. The default creates a **draft**, which you can still delete.
3. `--publish` publishes. On production the command asks you to type `PUBLISH`, unless you pass `--yes`.

## Mirror a list of DOIs

`batch` reads a JSON array. Each entry needs a `doi`. Every other field is optional, and `files` holds local paths.

```json
[
  {
    "doi": "10.5555/example-chapter",
    "files": ["files/example-chapter.pdf", "files/example-chapter.html"],
    "community": "my-community",
    "description": "<p>An optional description in HTML.</p>",
    "related": [{ "relation": "isPartOf", "identifier": "10.5555/example-book" }]
  }
]
```

The state file records one row per DOI: `draft`, `published`, `submitted`, `exists`, or `error`. A second run skips the rows that finished, and retries the rows that failed.

## How the metadata maps

DataCite answers first. If DataCite does not know the DOI, Crossref answers. Both registries map onto the same fields.

| Source (DataCite / Crossref)                      | Zenodo                                        |
| ------------------------------------------------- | --------------------------------------------- |
| `titles[0].title` / `title[0]`                    | `title`                                       |
| `creators` / `author` and `editor` (name, ORCID)  | `creators`                                    |
| `types.resourceTypeGeneral` / `type`              | `upload_type` and `publication_type`          |
| `rightsList[].rightsIdentifier` / `license[].URL` | `license`                                     |
| `Issued` date or `publicationYear` / `issued`     | `publication_date`, as a date or `YYYY-01-01` |
| `publisher`                                       | `imprint_publisher`, for publications         |
| `relatedIdentifiers`                              | `related_identifiers`                         |
| `url` / `resource.primary.URL`                    | `related_identifiers`, as `isIdenticalTo`     |
| DOI                                               | `doi`, kept unchanged                         |

Zenodo rejects a record with an empty description. Many DataCite records carry none, so the tool builds a short one from the title, the publisher, and the links. Pass `--description` to replace it.

## Upload a `.zenodo.json`

`upload` takes any Zenodo deposit metadata, including a [`.zenodo.json`](https://help.zenodo.org/docs/github/describe-software/zenodo-json/) file. The fields sit at the top level, or inside `{"metadata": {...}}`.

Every field goes to Zenodo unchanged, so Zenodo-specific fields such as `version`, `access_right`, `contributors`, and `grants` survive. Five fields are required: `title`, `upload_type`, `description`, `publication_date`, and a `name` for each creator. A missing field fails with a clear message, before any request is sent.

Omit the `doi` field to let Zenodo mint a DOI. Include it to keep an identifier that you already own. See [`examples/.zenodo.json`](examples/.zenodo.json).

## Submit to a community

A record does not join a community through its metadata. Zenodo stores the legacy `communities` deposit field but never acts on it. A record joins through a review request instead, and this tool creates that request for you.

| `--community` | `--publish` | Result                                 | State file  |
| ------------- | ----------- | -------------------------------------- | ----------- |
| no            | no          | a private draft                        | `draft`     |
| no            | yes         | a published record                     | `published` |
| yes           | no          | a private draft with an unsent request | `draft`     |
| yes           | yes         | a request that waits for a curator     | `submitted` |

A submitted request is not a published record. A curator of the community must accept it in the community's *Requests* tab, and that acceptance publishes the record. While the request is open, Zenodo refuses to publish the draft and refuses to delete it.

## Examples

See [`examples/`](examples/) for a DataCite DOI, a Crossref DOI, and a `.zenodo.json` file.

## Develop

```bash
uv run pytest            # tests and doctests, 100% coverage enforced
uv run ruff check .      # lint
uv run ruff format .     # format
uv run ty check          # types
uv run prek run --all-files
```

Read [AGENTS.md](AGENTS.md) for the tooling specification. Commits follow [Conventional Commits](https://www.conventionalcommits.org/). Run `uv run cz commit` to write one. [git-cliff](https://git-cliff.org/) generates the changelog.

## Contribute

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

To report a vulnerability, read [SECURITY.md](SECURITY.md). Report it privately.

## Cite

To cite this software, use the metadata in [CITATION.cff](CITATION.cff). GitHub renders it behind the "Cite this repository" button.

## License

Code: [AGPL-3.0-only](LICENSE).
