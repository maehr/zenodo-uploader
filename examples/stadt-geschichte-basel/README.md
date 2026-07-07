# Mirroring Stadt.Geschichte.Basel to Zenodo

Mirrors all 88 DOIs of the nine-volume book series [Stadt.Geschichte.Basel](https://emono.unibas.ch/stadtgeschichtebasel/catalog) (Christoph Merian Verlag, CC BY-NC 4.0) into the Zenodo community [stadt-geschichte-basel](https://zenodo.org/communities/stadt-geschichte-basel/):

- **79 chapters** — one record per chapter DOI (`10.21255/sgb-0X.YY-…`) with the chapter PDF and the [minimal-HTML edition](https://github.com/Stadt-Geschichte-Basel/sgb-minimal-html) attached.
- **9 volumes** — one record per volume DOI (`10.21255/sgb-0X-…`) with the official full-volume PDF (or, without a local source tree, a merged bundle of the chapter PDFs).

All records keep their existing 10.21255 DOIs (Zenodo does not mint new ones) and carry `isPartOf`/`hasPart` relations plus a link to the emono landing page.

## Steps

```bash
# 1. Build manifest.json. With a local sgb-minimal-html checkout (chapter PDFs
#    under pdf/, incl. the official full-volume PDFs), nothing is downloaded:
uv run mirror_sgb.py --source /path/to/sgb-minimal-html
#    Without --source, PDFs/HTML are downloaded from emono/GitHub and volume
#    bundles are merged from the chapter PDFs.

# 2. Inspect the metadata mapping without writing anything
uv run zenodo-uploader batch --manifest manifest.json --dry-run | less

# 3. Rehearse on the sandbox (needs ZENODO_SANDBOX_TOKEN)
uv run zenodo-uploader batch --manifest manifest.json --state sandbox-state.json \
    --sandbox --limit 3 --publish --yes

# 4. Production: create drafts first, spot-check them in the Zenodo UI …
uv run zenodo-uploader batch --manifest manifest.json --state state.json

# 5. … then publish (asks for confirmation; this is permanent)
uv run zenodo-uploader batch --manifest manifest.json --state state.json --publish
```

After publishing, a curator has to accept the 88 community inclusion requests in the community's [Requests](https://zenodo.org/communities/stadt-geschichte-basel/requests) tab.

Note: the sandbox has no `stadt-geschichte-basel` community; either create one there or strip the `community` keys from the manifest for rehearsal.
