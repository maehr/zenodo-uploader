---
name: zenodo
description: Create, update, version, publish, and delete records on Zenodo. Use when the user wants to deposit, archive, or publish something on Zenodo or the Zenodo sandbox, to change or re-version a record that is already there, to mirror an existing DOI, or when the user asks about a .zenodo.json file, a Zenodo community submission, or a Zenodo API token.
license: AGPL-3.0-only
compatibility: Needs either the zenodo MCP server or the zenodo-uploader CLI, plus a Zenodo API token.
---

# Work with records on Zenodo

Zenodo is a repository for research output. It gives each record a DOI.

One record is a **deposition**. A deposition moves through a lifecycle:

```
create ──> add files ──> update metadata ──> publish
                                               │
                                               ├──> update      (metadata only)
                                               └──> new version (files and metadata)
```

You can create a deposition in two ways. Supply your own metadata, or mirror a
DOI that already exists somewhere else. Mirroring is one way in, not the main
purpose.

## Pick your tool first

Two interfaces do the same work. Check for the MCP server before you write a
shell command.

1. Look for tools named `mcp__zenodo__*` in your tool list.
2. If they exist, use them. Skip the rest of this section.
3. If they do not exist, use the command line.

Run the command line through `uvx`, which needs no install:

```bash
uvx --from zenodo-uploader zenodo --help
```

This table maps one interface to the other.

| Job                                  | MCP tool              | CLI command                         |
| ------------------------------------ | --------------------- | ----------------------------------- |
| Show what a DOI maps to              | `preview_doi`         | `create --from-doi DOI --dry-run`   |
| Ask whether a DOI is already there   | `check_doi`           | `check DOI`                         |
| Read one deposition                  | `get_deposition`      | not available                       |
| List the files of a deposition       | `list_files`          | `files ls ID`                       |
| Create from your own metadata        | `create_record`       | `create --metadata FILE`            |
| Create by mirroring a DOI            | `create_record`       | `create --from-doi DOI`             |
| Change the metadata                  | `update_record`       | `update ID --metadata FILE`         |
| Add files                            | `add_files`           | `files add ID FILE…`                |
| Remove a file                        | `remove_file`         | `files rm ID NAME`                  |
| Publish a draft                      | `publish_record`      | `publish ID`                        |
| Make a new version                   | `new_version`         | `new-version ID`                    |
| Send a draft to a community          | `submit_to_community` | `submit ID --community SLUG`        |
| Delete a draft                       | `delete_draft`        | `delete ID`                         |
| Create many records from a list      | not available         | `batch --manifest FILE`             |

## Set up the token

The user needs a personal access token. Give them these steps.

1. Open <https://zenodo.org/account/settings/applications/tokens/new/>.
2. Select the scopes `deposit:write` and `deposit:actions`.
3. Create the token, then copy it.
4. Set `ZENODO_TOKEN` in the environment, or write it into a `.env` file.

The sandbox is a separate service with separate accounts. To test, the user
must register again at <https://sandbox.zenodo.org/> and set
`ZENODO_SANDBOX_TOKEN`.

Never print a token. Never write a token into a file that the user commits.

## The safety ladder

**Caution: a published record on zenodo.org is permanent. Zenodo cannot delete
it, and you cannot undo the step.**

Climb one rung at a time. Stop at the rung the user asked for.

1. **Preview.** Run `preview_doi`, or `create --from-doi DOI --dry-run`. This
   writes nothing.
2. **Show the result to the user.** Check the title, the creators, the date,
   and the licence.
3. **Draft on the sandbox.** Keep `sandbox` true. A draft is private, and you
   can delete it.
4. **Publish on the sandbox.** Confirm that the record looks correct.
5. **Draft on production.** Set `sandbox` to false, and leave `publish` off.
6. **Publish on production.** Ask the user first, in plain words. Then set
   `publish`.

The MCP server refuses a production publish unless two conditions are true.

- The operator started the server with `ZENODO_ALLOW_PRODUCTION_PUBLISH=1`.
- The call passes `confirm="PUBLISH"`, or `confirm="DELETE"` to delete a draft.

If the server refuses, do not look for a way around it. Tell the user what the
server needs.

The CLI asks the user to type `PUBLISH`, or `DELETE`, at the terminal. The
`--yes` option skips that question. Do not pass `--yes` unless the user asks
for it.

## Create a record

Follow these steps.

1. Decide the source. Use your own metadata for new material. Use a DOI only to
   mirror something that already exists.
2. If you mirror, run `check_doi` first. If the DOI is already on Zenodo, stop
   and report the record.
3. Collect the files. Give absolute paths.
4. Create the record with `sandbox` true.
5. Report the deposition id and the link.

### Metadata that Zenodo requires

Zenodo rejects a record that misses one of these fields:

- `title`
- `upload_type`
- `description`
- `publication_date`
- a `name` for each creator

Every other field goes to Zenodo unchanged, so Zenodo-specific fields such as
`version`, `access_right`, `contributors`, and `grants` all survive.

### Keep the DOI or mint a new one

Omit the `doi` field and Zenodo mints a DOI for you. Include it to keep an
identifier that you already own.

When you mirror, the tool keeps the original DOI by default, so the mirror
stays citable under one identifier. Pass `--mint-doi`, or `keep_doi=false`, to
mint a new one instead.

Many DataCite records carry no description. The tool then builds a short one
from the title, the publisher, and the links.

## Change a record that already exists

**This is the rule that matters most.** Which command works depends on who
minted the DOI. The two commands cover opposite cases, and each one refuses the
other's case with a message that names the right command.

| The record                                | Change the metadata | Add or change files |
| ----------------------------------------- | ------------------- | ------------------- |
| Draft, not yet published                   | `update_record`     | `add_files`         |
| Published, DOI minted by Zenodo            | **`new_version`**   | `new_version`       |
| Published, DOI from somewhere else         | **`update_record`** | `new_version` fails |

Zenodo versions a record through its concept DOI, and it creates a concept DOI
only for a DOI it minted itself. A mirrored record keeps an external DOI, so it
has no concept DOI and Zenodo cannot version it. In return, Zenodo lets you
edit and publish that record again, which it refuses for its own DOIs.

A new version keeps the concept DOI, so the record stays one citable series.
Only one unpublished new version can exist at a time.

**Never create a second record to correct a published one.** That mints a
duplicate. Use `new_version`, or `update_record`, as the table says.

## Submit to a community

A community is a collection that a curator manages. A record does not join a
community through its metadata. Zenodo stores the legacy `communities` field,
but Zenodo ignores it.

A record joins a community through a review request. The tool creates that
request for you.

| `community` | `publish` | Result                                   | Status      |
| ----------- | --------- | ---------------------------------------- | ----------- |
| no          | no        | a private draft                          | `draft`     |
| no          | yes       | a published record                       | `published` |
| yes         | no        | a private draft with an unsent request   | `draft`     |
| yes         | yes       | a request that waits for a curator       | `submitted` |

Tell the user what `submitted` means. The record is not public. A curator of
the community must accept the request in the *Requests* tab. The acceptance
publishes the record.

While the request is open, Zenodo refuses to publish the draft and refuses to
delete it.

## Create many records

The MCP server has no batch tool. Call `create_record` once per record, or use
the CLI.

The CLI reads a JSON array. Each entry gives exactly one of `doi`, `metadata`,
or `metadata_file`, so one list can mix mirrored DOIs with records of your own.

```json
[
  { "doi": "10.5555/example-chapter", "files": ["files/chapter.pdf"] },
  { "id": "dataset-2024", "metadata_file": "records/ds.json", "files": ["data.csv"] },
  { "id": "poster-2024",
    "metadata": { "title": "Poster", "upload_type": "poster",
                  "description": "<p>A poster.</p>", "publication_date": "2024-01-01",
                  "creators": [{ "name": "Doe, Jane" }] },
    "files": ["poster.pdf"] }
]
```

Run the list with a state file:

```bash
uvx --from zenodo-uploader zenodo batch \
    --manifest manifest.json --state state.json --sandbox
```

The state file records one row per entry, keyed by `id`, else the DOI, else the
file path. A second run skips the rows that finished, and retries the rows that
failed. Pass `--limit N` to process only N entries.

**Caution: keep the state file.** A DOI entry is also checked against Zenodo
itself, so it stays safe to rerun. An entry that carries its own metadata has
nothing to check against. Delete the state file, rerun, and you create those
records a second time.

## When something fails

| Message                          | Cause                            | Action                                              |
| -------------------------------- | -------------------------------- | --------------------------------------------------- |
| `ZENODO_TOKEN is not set`        | No token in the environment      | Ask the user to set the token. See "Set up the token". |
| `cannot resolve DOI`             | No registry knows the DOI        | Check the DOI. Supply metadata instead.             |
| `missing required field`         | The metadata is incomplete       | Add the fields that "Metadata that Zenodo requires" lists. |
| `Refusing to publish`            | The production guard stopped you | Publish on the sandbox, or ask the operator.        |
| `Refusing to delete`             | The production guard stopped you | Delete on the sandbox, or ask the operator.         |
| `use new_version instead`        | The DOI is one Zenodo minted     | Run `new_version`. See the table above.             |
| `use update_record instead`      | The DOI is external              | Run `update_record`. See the table above.           |
| `open review request`            | A community request is open      | Ask a curator to accept the request.                |
| `Please remove all files first`  | An unpublished new version exists| Publish it, or delete it, then try again.           |

Report a failure to the user with the message that the tool gave. Do not retry
a write that failed for a reason you cannot fix.
