# Examples

`zenodo-uploader` works with any DOI. It resolves metadata from **DataCite**
first and falls back to **Crossref**, so books, chapters, journal articles,
datasets, and preprints all map onto Zenodo deposit metadata the same way.

## One-off DOIs

Preview the mapping without touching Zenodo (`--dry-run` makes no writes):

```bash
# A Brill monograph (metadata comes from Crossref)
uvx --from zenodo-uploader zenodo-uploader from-doi 10.30965/9783657796823 --dry-run

# A journal article in the Zeitschrift für digitale Geisteswissenschaften
# (metadata comes from DataCite)
uvx --from zenodo-uploader zenodo-uploader from-doi 10.17175/2026_006 --dry-run
```

Both resolve automatically — you do not tell the tool which registry a DOI
belongs to. The Crossref record yields `upload_type: publication`,
`publication_type: book`, the full publication date, publisher, and the
publisher landing page as an `isIdenticalTo` relation; the DataCite record
yields `publication_type: article` with ORCID-tagged authors.

Attach files and push it to the sandbox as a draft:

```bash
uvx --from zenodo-uploader zenodo-uploader from-doi 10.30965/9783657796823 \
    --file cover.pdf --sandbox
```

Then finish (add remaining files, publish) in the Zenodo web UI, or add
`--publish` (sandbox) / `--publish` with the interactive `PUBLISH` confirmation
(production).

## Uploading a `.zenodo.json` file

If you already have Zenodo metadata — for example a
[`.zenodo.json`](https://help.zenodo.org/docs/github/describe-software/zenodo-json/)
describing your software — upload it directly, no DOI resolution involved:

```bash
# Preview the exact payload (no writes)
uvx --from zenodo-uploader zenodo-uploader upload --metadata .zenodo.json --dry-run

# Create a draft on the sandbox with a file attached
uvx --from zenodo-uploader zenodo-uploader upload --metadata .zenodo.json --file dist.zip --sandbox
```

All fields are sent to Zenodo verbatim, including Zenodo-specific ones
(`version`, `access_right`, `contributors`, `grants`, `communities`, …). Because
there is **no `doi` field** in the file, Zenodo mints a fresh DOI on publish;
add a `doi` field to keep an existing one instead. `title`, `upload_type`,
`description`, `publication_date`, and each creator's `name` are required — a
missing one fails with a clear message before anything is sent. See
[`.zenodo.json`](.zenodo.json) for a complete example.

## Batches

For many DOIs, write a `manifest.json` (a JSON array of `{doi, files, ...}`)
and run `zenodo-uploader batch`. The state file makes the run resumable:

```bash
uvx --from zenodo-uploader zenodo-uploader batch --manifest manifest.json --state state.json --sandbox
```

## From an agent

The same jobs run through the MCP server. `preview_doi` replaces `--dry-run`,
`mirror_doi` replaces `from-doi`, and `upload_record` replaces `upload`. See the
[README](../README.md) for the full tool list.
