---
name: zenodo
description: Mirror a DOI to Zenodo, or upload a new record with files. Use when the user wants to deposit, archive, mirror, or publish something on Zenodo or the Zenodo sandbox, when the user names a DOI to put on Zenodo, or when the user asks about a .zenodo.json file, a Zenodo community submission, or a Zenodo API token.
license: AGPL-3.0-only
compatibility: Needs either the zenodo MCP server or the zenodo-uploader CLI, plus a Zenodo API token.
---

# Deposit records on Zenodo

Zenodo is a repository for research output. It gives each record a DOI.

This skill covers two jobs:

- **Mirror a DOI.** Take an existing DOI, read its metadata from DataCite or Crossref, and create the same record on Zenodo. The record keeps its original DOI.
- **Upload a record.** Take Zenodo metadata that you already have, attach files, and create the record.

## Pick your tool first

Two interfaces do the same work. Check for the MCP server before you write a shell command.

1. Look for tools named `mcp__zenodo__*` in your tool list.
2. If they exist, use them. Skip the rest of this section.
3. If they do not exist, use the command line.

Run the command line through `uvx`, which needs no install:

```bash
uvx --from zenodo-uploader zenodo-uploader --help
```

This table maps one interface to the other.

| Job                                  | MCP tool              | CLI command                      |
| ------------------------------------ | --------------------- | -------------------------------- |
| Show the metadata a DOI maps to      | `preview_doi`         | `from-doi DOI --dry-run`         |
| Ask whether a DOI is already there   | `check_doi`           | `check DOI`                      |
| Mirror a DOI                         | `mirror_doi`          | `from-doi DOI`                   |
| Upload metadata you already have     | `upload_record`       | `upload --metadata FILE`         |
| Send a draft to a community          | `submit_to_community` | not available                    |
| Read one deposition                  | `get_deposition`      | not available                    |
| Mirror many DOIs from a list         | not available         | `batch --manifest FILE`          |

## Set up the token

The user needs a personal access token. Give them these steps.

1. Open <https://zenodo.org/account/settings/applications/tokens/new/>.
2. Select the scopes `deposit:write` and `deposit:actions`.
3. Create the token, then copy it.
4. Set `ZENODO_TOKEN` in the environment, or write it into a `.env` file.

The sandbox is a separate service with separate accounts. To test, the user must register again at <https://sandbox.zenodo.org/> and set `ZENODO_SANDBOX_TOKEN`.

Never print a token. Never write a token into a file that the user commits.

## The safety ladder

**Caution: a published record on zenodo.org is permanent. Zenodo cannot delete it, and you cannot undo the step.**

Climb one rung at a time. Stop at the rung the user asked for.

1. **Preview.** Run `preview_doi`, or `from-doi DOI --dry-run`. This writes nothing.
2. **Show the result to the user.** Check the title, the creators, the date, and the licence.
3. **Draft on the sandbox.** Keep `sandbox` true. A draft is private, and you can delete it.
4. **Publish on the sandbox.** Confirm that the record looks correct.
5. **Draft on production.** Set `sandbox` to false, and leave `publish` off.
6. **Publish on production.** Ask the user first, in plain words. Then set `publish`.

The MCP server refuses a production publish unless two conditions are true.

- The operator started the server with `ZENODO_ALLOW_PRODUCTION_PUBLISH=1`.
- The call passes `confirm="PUBLISH"`.

If the server refuses, do not look for a way around it. Tell the user what the server needs.

The CLI asks the user to type `PUBLISH` at the terminal. The `--yes` option skips that question. Do not pass `--yes` unless the user asks for it.

## Mirror a DOI

Follow these steps.

1. Run `check_doi` first. If the DOI is already on Zenodo, stop and report the record.
2. Run `preview_doi` and show the metadata to the user.
3. Collect the files. Give absolute paths.
4. Run `mirror_doi` with `sandbox` true.
5. Report the deposition id and the link.

### Keep the DOI or mint a new one

The tool keeps the original DOI by default. The mirror then stays citable under one identifier.

Mint a new DOI only when the record is new material. To mint one, set `keep_doi` to false, or pass `--mint-doi`.

### Metadata that Zenodo requires

Zenodo rejects a record that misses one of these fields:

- `title`
- `upload_type`
- `description`
- `publication_date`
- a `name` for each creator

Many DataCite records carry no description. The tool then builds a short one from the title, the publisher, and the links. To replace it, pass your own `description`.

## Upload a record you already have

Use `upload_record`, or `upload --metadata FILE`. The input is Zenodo deposit metadata, such as a [`.zenodo.json`](https://help.zenodo.org/docs/github/describe-software/zenodo-json/) file.

- The fields go to Zenodo unchanged. Zenodo-specific fields such as `version`, `access_right`, `contributors`, and `grants` survive.
- Omit the `doi` field to let Zenodo mint a DOI.
- Include the `doi` field to keep an identifier that you already own.

## Submit to a community

A community is a collection that a curator manages. A record does not join a community through its metadata. Zenodo stores the legacy `communities` field, but Zenodo ignores it.

A record joins a community through a review request. The tool creates that request for you.

| `community` | `publish` | Result                                   | Status      |
| ----------- | --------- | ---------------------------------------- | ----------- |
| no          | no        | a private draft                          | `draft`     |
| no          | yes       | a published record                       | `published` |
| yes         | no        | a private draft with an unsent request   | `draft`     |
| yes         | yes       | a request that waits for a curator       | `submitted` |

Tell the user what `submitted` means. The record is not public. A curator of the community must accept the request in the *Requests* tab. The acceptance publishes the record.

While the request is open, Zenodo refuses to publish the draft and refuses to delete it.

## Mirror many DOIs

The MCP server has no batch tool. Call `mirror_doi` once per DOI, or use the CLI.

The CLI reads a JSON array. Each entry needs a `doi`. Every other field is optional.

```json
[
  {
    "doi": "10.5555/example-chapter",
    "files": ["files/example-chapter.pdf"],
    "community": "my-community",
    "description": "<p>An optional description in HTML.</p>",
    "related": [{ "relation": "isPartOf", "identifier": "10.5555/example-book" }]
  }
]
```

Run the batch with a state file:

```bash
uvx --from zenodo-uploader zenodo-uploader batch \
    --manifest manifest.json --state state.json --sandbox
```

The state file records one row per DOI. A second run skips the rows that finished, and retries the rows that failed. Pass `--limit N` to process only N entries.

## When something fails

| Message                          | Cause                            | Action                                              |
| -------------------------------- | -------------------------------- | --------------------------------------------------- |
| `ZENODO_TOKEN is not set`        | No token in the environment      | Ask the user to set the token. See "Set up the token". |
| `cannot resolve DOI`             | No registry knows the DOI        | Check the DOI. Use `upload_record` instead.         |
| `missing required field`         | The metadata is incomplete       | Add the fields that "Metadata that Zenodo requires" lists. |
| `Refusing to publish`            | The production guard stopped you | Publish on the sandbox, or ask the operator.        |
| `open review request`            | A community request is open      | Ask a curator to accept the request.                |

Report a failure to the user with the message that the tool gave. Do not retry a write that failed for a reason you cannot fix.
