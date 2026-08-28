# zenodo-uploader

[![CI](https://github.com/maehr/zenodo-uploader/actions/workflows/ci.yml/badge.svg)](https://github.com/maehr/zenodo-uploader/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.13-blue.svg)](pyproject.toml)

> A Zenodo client: create, update, version, and publish records, from an agent or from the command line.

<!-- mcp-name: io.github.maehr/zenodo-uploader -->

**zenodo-uploader** works with one thing: a Zenodo **deposition**. You create one from your own metadata, attach files, change it, publish it, and later publish a new version of it — all through the [Zenodo REST API](https://developers.zenodo.org/).

```
create ──> add files ──> update metadata ──> publish
                                               │
                                               ├──> update      (metadata only)
                                               └──> new version (files and metadata)
```

Mirroring an existing DOI is one way to create a deposition: give it a DOI and the metadata comes from [DataCite](https://api.datacite.org/) or [Crossref](https://api.crossref.org/), with the original identifier kept so the mirror stays citable. It is one source among several, not the point of the tool.

The project ships three interfaces over one core:

| Interface       | Use it when                                              |
| --------------- | -------------------------------------------------------- |
| **MCP server**  | An agent does the work. Twelve tools over stdio.         |
| **Agent Skill** | You use Claude Code and want the procedure as well.      |
| **CLI**         | You work at a terminal, or you create records in bulk.   |

## Install

> **Not on PyPI yet.** The `uvx --from zenodo-uploader …` commands below start to
> work with the first release. Until then, clone the repository and replace
> `uvx --from zenodo-uploader` with `uv run` inside the checkout.

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

The server offers twelve tools:

| Tool                  | Writes | Purpose                                               |
| --------------------- | ------ | ----------------------------------------------------- |
| `preview_doi`         | no     | Show the Zenodo metadata that a DOI maps to.          |
| `check_doi`           | no     | Report whether a DOI is already on Zenodo.            |
| `get_deposition`      | no     | Read the state, DOI, title, and link of a deposition. |
| `list_files`          | no     | List the files attached to a deposition.              |
| `create_record`       | yes    | Create from your own metadata, or by mirroring a DOI. |
| `update_record`       | yes    | Replace the metadata of a deposition.                 |
| `add_files`           | yes    | Add files to a deposition.                            |
| `remove_file`         | yes    | Remove one file from a draft.                         |
| `publish_record`      | yes    | Publish an existing draft.                            |
| `new_version`         | yes    | Open a new version of a published record.             |
| `submit_to_community` | yes    | Send a draft to a community for inclusion.            |
| `delete_draft`        | yes    | Delete a draft.                                       |

Every tool works against the sandbox unless you pass `sandbox: false`.

**Caution: a published record on zenodo.org is permanent.** The server therefore refuses a production publish unless both of these are true:

- you started the server with `ZENODO_ALLOW_PRODUCTION_PUBLISH=1`, and
- the call passes `confirm="PUBLISH"` (or `confirm="DELETE"` to delete a draft).

## Use it from the command line

The command is `zenodo` (`zenodo-uploader` also works).

```bash
# Create from your own metadata. Every field goes to Zenodo unchanged.
uvx --from zenodo-uploader zenodo create --metadata record.json --file data.csv --sandbox

# Create by mirroring an existing DOI, keeping that DOI
uvx --from zenodo-uploader zenodo create --from-doi 10.30965/9783657796823 --file chapter.pdf --sandbox

# Show what a DOI maps to. This writes nothing.
uvx --from zenodo-uploader zenodo create --from-doi 10.30965/9783657796823 --dry-run

# Work on an existing deposition
uvx --from zenodo-uploader zenodo files ls 1234567 --sandbox
uvx --from zenodo-uploader zenodo files add 1234567 extra.csv --sandbox
uvx --from zenodo-uploader zenodo files rm 1234567 old.csv --sandbox
uvx --from zenodo-uploader zenodo update 1234567 --metadata record.json --sandbox

# Publish it, then publish a new version later
uvx --from zenodo-uploader zenodo publish 1234567 --sandbox
uvx --from zenodo-uploader zenodo new-version 1234567 --file v2.csv --sandbox --publish

# Send a draft to a community, and clean up a draft you do not want
uvx --from zenodo-uploader zenodo submit 1234567 --community my-community --sandbox
uvx --from zenodo-uploader zenodo delete 1234567 --sandbox

# Ask whether a DOI is already mirrored
uvx --from zenodo-uploader zenodo check 10.30965/9783657796823

# Create many records from one list, resumably
uvx --from zenodo-uploader zenodo batch --manifest manifest.json --state state.json --sandbox
```

### The safety ladder

Climb one rung at a time.

1. `--dry-run` prints the mapped metadata and writes nothing.
2. The default creates a **draft**, which you can still delete.
3. `--publish` publishes. On production the command asks you to type `PUBLISH`, unless you pass `--yes`.

## Create many records at once

`batch` reads a JSON array. Each entry gives exactly **one** of `doi`, `metadata`, or `metadata_file`, so one list can mix mirrored DOIs with records of your own.

```json
[
  { "doi": "10.30965/9783657796823", "files": ["files/chapter.pdf"] },
  { "id": "dataset-2024", "metadata_file": "records/ds.json", "files": ["data.csv"] },
  {
    "id": "poster-2024",
    "metadata": {
      "title": "Poster",
      "upload_type": "poster",
      "description": "<p>A poster.</p>",
      "publication_date": "2024-01-01",
      "creators": [{ "name": "Doe, Jane" }]
    },
    "files": ["poster.pdf"]
  }
]
```

The state file records one row per entry, keyed by `id`, else the DOI, else the file path. A duplicate key is rejected when the manifest loads, before any request goes out. A second run skips the rows that finished (`published`, `submitted`, `exists`) and retries the rows that failed.

**Caution: keep the state file.** A DOI entry is also checked against Zenodo itself, so it is safe to rerun. An entry that carries its own metadata has nothing to check against, so the state file is its only guard. Delete it, rerun, and those records are created a second time.

## How a mirrored DOI maps

This applies to `--from-doi` only. DataCite answers first. If DataCite does not know the DOI, Crossref answers. Both registries map onto the same fields.

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

## Supply your own metadata

`create --metadata` takes any Zenodo deposit metadata, including a [`.zenodo.json`](https://help.zenodo.org/docs/github/describe-software/zenodo-json/) file. The fields sit at the top level, or inside `{"metadata": {...}}`.

Every field goes to Zenodo unchanged, so Zenodo-specific fields such as `version`, `access_right`, `contributors`, and `grants` survive. Five fields are required: `title`, `upload_type`, `description`, `publication_date`, and a `name` for each creator. A missing field fails with a clear message, before any request is sent.

Omit the `doi` field to let Zenodo mint a DOI. Include it to keep an identifier that you already own. See [`examples/.zenodo.json`](examples/.zenodo.json).

## Change a record that already exists

Which command works depends on who minted the DOI. The two cover opposite cases, and each refuses the other's case with a message naming the right one.

| The record                        | Change the metadata | Add or change files |
| --------------------------------- | ------------------- | ------------------- |
| Draft, not yet published          | `update`            | `files add`         |
| Published, DOI minted by Zenodo   | **`new-version`**   | `new-version`       |
| Published, DOI from somewhere else| **`update`**        | `new-version` fails |

Zenodo versions a record through its *concept DOI*, and it creates one only for a DOI it minted itself. A mirrored record keeps an external DOI, so it has no concept DOI and Zenodo will not version it. In exchange, Zenodo lets you edit and re-publish that record, which it refuses for its own DOIs.

A new version keeps the concept DOI, so the record stays one citable series. Only one unpublished new version can exist at a time.

**Never create a second record to correct a published one** — that mints a duplicate.

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
