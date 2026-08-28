"""MCP server exposing the Zenodo deposition lifecycle to any MCP host.

Runs over stdio. Every tool defaults to the sandbox, and anything irreversible
on production needs two independent confirmations; see :func:`_guard_production`.

Start it with::

    uvx --from zenodo-uploader zenodo-mcp
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any

import httpx2
import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .batch import ManifestEntry, process_entry
from .config import Settings, base_url_for
from .mapping import work_to_zenodo
from .models import RelatedIdentifier, validate_deposit_metadata
from .resolve import fetch_work
from .zenodo import ZenodoClient, ZenodoError, is_zenodo_doi

# stdio is the transport: anything on stdout corrupts the protocol stream.
structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))

server = MCPServer(
    name="zenodo",
    instructions=(
        "Create, update, version, and publish Zenodo records. Mirroring an existing "
        "DOI is one way to create one: pass doi to create_record. Every tool works "
        "against sandbox.zenodo.org unless sandbox=false. Anything irreversible on "
        "production needs both the ZENODO_ALLOW_PRODUCTION_PUBLISH=1 environment "
        "variable and the matching confirm word."
    ),
)

ALLOW_PRODUCTION_PUBLISH_ENV = "ZENODO_ALLOW_PRODUCTION_PUBLISH"

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# destructive_hint is per tool, not per call. Each of these can publish or
# remove something that cannot be brought back, so the annotation must let a
# host prompt rather than auto-approve.
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

SandboxArg = Annotated[
    bool,
    Field(description="Use sandbox.zenodo.org. Set false only for the real zenodo.org."),
]
ConfirmArg = Annotated[
    str | None,
    Field(description="Must be the literal 'PUBLISH' to publish on production zenodo.org."),
]
DepositionArg = Annotated[int, Field(description="Deposition id of the record.")]
FilesArg = Annotated[list[str] | None, Field(description="Absolute paths of files to attach.")]


def _guard_production(sandbox: bool, publish: bool, confirm: str | None) -> None:
    """Refuse a production publish that is not doubly confirmed.

    A CLI can ask a human at the terminal. An MCP server has no terminal, so
    the operator opts in once through the environment and the caller confirms
    again per call. Publishing on zenodo.org cannot be undone.

    Examples:
        >>> _guard_production(sandbox=True, publish=True, confirm=None)
        >>> _guard_production(sandbox=False, publish=False, confirm=None)
    """
    _guard(sandbox, publish, confirm, action="publish on", word="PUBLISH")


def _guard_production_delete(sandbox: bool, confirm: str | None) -> None:
    """Refuse a production delete that is not doubly confirmed.

    Examples:
        >>> _guard_production_delete(sandbox=True, confirm=None)
    """
    _guard(sandbox, True, confirm, action="delete a draft on", word="DELETE")


def _guard(sandbox: bool, risky: bool, confirm: str | None, *, action: str, word: str) -> None:
    """Common body of the two production guards."""
    if sandbox or not risky:
        return
    if os.environ.get(ALLOW_PRODUCTION_PUBLISH_ENV) != "1":
        raise ToolError(
            f"Refusing to {action} production zenodo.org: the server was not started "
            f"with {ALLOW_PRODUCTION_PUBLISH_ENV}=1. Work on the sandbox instead "
            "(sandbox=true), or ask the operator to restart the server with that "
            "variable set."
        )
    if confirm != word:
        raise ToolError(
            f"Refusing to {action} production zenodo.org: pass confirm='{word}'. "
            "The change cannot be undone."
        )


def _client(sandbox: bool) -> ZenodoClient:
    """Build a Zenodo client, turning a missing token into a tool error."""
    try:
        token = Settings().token_for(sandbox)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return ZenodoClient(base_url_for(sandbox), token)


def _registry_client() -> httpx2.Client:
    return httpx2.Client(timeout=30, follow_redirects=True)


def _summarise(deposition: dict[str, Any]) -> dict[str, Any]:
    """Trim a Zenodo deposition to the fields a caller acts on.

    Raw deposition payloads are large and mostly noise for an agent.

    Examples:
        >>> _summarise({"id": 1, "state": "done", "links": {"html": "https://z/1"},
        ...     "metadata": {"doi": "10.1/x", "title": "T"}, "noise": [1, 2, 3]})
        {'id': 1, 'state': 'done', 'doi': '10.1/x', 'title': 'T', 'url': 'https://z/1'}
    """
    metadata = deposition.get("metadata", {})
    links = deposition.get("links", {})
    return {
        "id": deposition.get("id"),
        "state": deposition.get("state"),
        "doi": deposition.get("doi") or metadata.get("doi"),
        "title": metadata.get("title"),
        "url": links.get("html") or links.get("self_html"),
    }


def _paths(files: list[str] | None) -> list[Path]:
    """Turn caller-supplied path strings into paths, rejecting a missing file."""
    paths = [Path(f) for f in files or []]
    for path in paths:
        if not path.is_file():
            raise ToolError(f"file not found: {path}")
    return paths


# --- read-only ---------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
def preview_doi(
    doi: Annotated[str, Field(description="DOI to resolve, e.g. 10.5555/example-book.")],
    description: Annotated[
        str | None, Field(description="Override the synthesized HTML description.")
    ] = None,
    keep_doi: Annotated[
        bool, Field(description="Keep the existing DOI instead of minting a new one.")
    ] = True,
) -> dict[str, Any]:
    """Resolve a DOI and show the Zenodo metadata it maps to. Writes nothing.

    Metadata comes from DataCite, falling back to Crossref. Run this before
    create_record so the caller can check the mapping first.
    """
    try:
        with _registry_client() as registry:
            record = fetch_work(registry, doi)
    except httpx2.HTTPError as exc:
        # HTTPError covers both a bad status and a transport failure such as a
        # timeout, so neither escapes as an unhandled exception.
        raise ToolError(f"cannot resolve {doi}: {exc}") from exc
    return work_to_zenodo(record, description=description, keep_doi=keep_doi).to_payload()[
        "metadata"
    ]


@server.tool(annotations=READ_ONLY)
def check_doi(
    doi: Annotated[str, Field(description="DOI to look up on Zenodo.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Report whether a DOI is already on Zenodo, as a record or as your own draft.

    Use this before creating a record to avoid a duplicate.
    """
    try:
        with _client(sandbox) as client:
            records = client.find_records_by_doi(doi)
            drafts = client.find_depositions_by_doi(doi)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "doi": doi,
        "published": [r.get("links", {}).get("html") for r in records],
        "drafts": [d.get("id") for d in drafts],
    }


@server.tool(annotations=READ_ONLY)
def get_deposition(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Fetch one deposition's current state, DOI, title, and link."""
    try:
        with _client(sandbox) as client:
            return _summarise(client.get_deposition(deposition_id))
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=READ_ONLY)
def list_files(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
) -> list[dict[str, Any]]:
    """List the files attached to a deposition."""
    try:
        with _client(sandbox) as client:
            listed = client.list_files(deposition_id)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc
    return [{"filename": f.get("filename"), "size": f.get("filesize")} for f in listed]


# --- writes ------------------------------------------------------------------


@server.tool(annotations=WRITES)
def create_record(
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Zenodo deposit metadata, as in a .zenodo.json file."),
    ] = None,
    doi: Annotated[
        str | None,
        Field(description="Mirror this existing DOI instead of supplying metadata."),
    ] = None,
    files: FilesArg = None,
    community: Annotated[
        str | None,
        Field(description="Community slug. Attaches an inclusion request, not metadata."),
    ] = None,
    description: Annotated[
        str | None, Field(description="Override the description (doi path only).")
    ] = None,
    related: Annotated[
        dict[str, str] | None,
        Field(description="Extra related identifiers as {relation: identifier}."),
    ] = None,
    sandbox: SandboxArg = True,
    publish: Annotated[
        bool,
        Field(
            description="Publish, or submit the community request, instead of stopping at a draft."
        ),
    ] = False,
    confirm: ConfirmArg = None,
) -> dict[str, Any]:
    """Create a deposition from your own metadata, or by mirroring a DOI.

    Give exactly one of `metadata` or `doi`. With `metadata` every field goes to
    Zenodo unchanged, so a `.zenodo.json` works as it is; omit its `doi` field
    to have Zenodo mint one. With `doi` the metadata comes from DataCite or
    Crossref and the existing DOI is kept, so the mirror stays citable.

    A community is not metadata. With `community` this attaches a
    community-submission review; with `publish` as well it submits that review
    as an inclusion request a curator must accept. The status is then
    `submitted`, not `published` — the record is not public yet.
    """
    if (metadata is None) == (doi is None):
        raise ToolError("give exactly one of metadata or doi")
    _guard_production(sandbox, publish, confirm)
    try:
        if metadata is not None:
            meta = metadata.get("metadata", metadata)
            validate_deposit_metadata(meta)
            entry = ManifestEntry(metadata=meta, files=_paths(files), community=community)
        else:
            entry = ManifestEntry(
                doi=doi,
                files=_paths(files),
                description=description,
                community=community,
                related=[
                    RelatedIdentifier.model_validate({"relation": k, "identifier": v})
                    for k, v in (related or {}).items()
                ],
            )
    except (ValueError, AttributeError) as exc:
        raise ToolError(f"invalid arguments: {exc}") from exc
    try:
        with _client(sandbox) as client, _registry_client() as registry:
            row = dict(process_entry(client, registry, entry, publish=publish))
    except (ZenodoError, httpx2.HTTPError) as exc:
        raise ToolError(str(exc)) from exc
    # Report the same key every other tool reports, and drop the timestamp that
    # only the state file needs.
    row.pop("timestamp", None)
    if "deposition_id" in row:
        row["id"] = row.pop("deposition_id")
    return row


@server.tool(annotations=WRITES)
def update_record(
    deposition_id: DepositionArg,
    metadata: Annotated[dict[str, Any], Field(description="Replacement Zenodo deposit metadata.")],
    sandbox: SandboxArg = True,
    confirm: ConfirmArg = None,
) -> dict[str, Any]:
    """Replace the metadata of a deposition.

    On a draft this is a plain update. On a published record it unlocks the
    record, updates it, and publishes it again.

    A published record whose DOI Zenodo minted cannot be published a second
    time: Zenodo rejects it. This tool refuses that case before changing
    anything and tells you to use new_version instead. The two tools cover
    opposite cases.
    """
    meta = metadata.get("metadata", metadata)
    try:
        validate_deposit_metadata(meta)
    except (ValueError, AttributeError) as exc:
        raise ToolError(str(exc)) from exc
    try:
        with _client(sandbox) as client:
            current = client.get_deposition(deposition_id)
            published = current.get("state") in ("done", "inprogress")
            if published and is_zenodo_doi(current.get("doi")):
                raise ToolError(
                    f"record {deposition_id} is published with a Zenodo-minted DOI "
                    f"({current.get('doi')}), which Zenodo refuses to publish again. "
                    f"Use new_version on {deposition_id} instead."
                )
            if not published:
                client.update_deposition(deposition_id, meta)
                return _summarise(client.get_deposition(deposition_id)) | {"status": "draft"}
            _guard_production(sandbox, True, confirm)
            client.edit_deposition(deposition_id)
            try:
                client.update_deposition(deposition_id, meta)
                record = client.publish(deposition_id)
            except Exception:
                # Never leave the record stuck in an open edit session.
                client.discard_edit(deposition_id)
                raise
            return _summarise(record) | {"status": "republished"}
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=WRITES)
def new_version(
    deposition_id: DepositionArg,
    files: FilesArg = None,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Replacement metadata for the new version."),
    ] = None,
    sandbox: SandboxArg = True,
    publish: Annotated[bool, Field(description="Publish the new version.")] = False,
    confirm: ConfirmArg = None,
) -> dict[str, Any]:
    """Open a new version of a published record.

    The new draft inherits the previous version's files and keeps the concept
    DOI, so the record stays one citable series. Only one unpublished new
    version can exist at a time.

    Zenodo versions a record through its concept DOI, which exists only for a
    DOI Zenodo minted. A record that kept an external DOI has none, so this
    tool refuses it and tells you to use update_record instead.
    """
    _guard_production(sandbox, publish, confirm)
    paths = _paths(files)
    try:
        with _client(sandbox) as client:
            current = client.get_deposition(deposition_id)
            if not current.get("conceptdoi"):
                raise ToolError(
                    f"record {deposition_id} has no concept DOI, because its DOI "
                    f"({current.get('doi')}) is not one Zenodo minted. Zenodo cannot "
                    f"version it. Use update_record on {deposition_id} instead."
                )
            draft = client.new_version(deposition_id)
            draft_id = int(draft["id"])
            if metadata is not None:
                client.update_deposition(draft_id, metadata.get("metadata", metadata))
            for path in paths:
                client.upload_file(draft, path)
            if publish:
                return _summarise(client.publish(draft_id)) | {"of": deposition_id}
            return _summarise(client.get_deposition(draft_id)) | {"of": deposition_id}
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=WRITES)
def add_files(
    deposition_id: DepositionArg,
    files: Annotated[list[str], Field(description="Absolute paths of files to upload.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Add files to a deposition, overwriting a file of the same name."""
    paths = _paths(files)
    try:
        with _client(sandbox) as client:
            deposition = client.get_deposition(deposition_id)
            for path in paths:
                client.upload_file(deposition, path)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc
    return {"id": deposition_id, "added": [p.name for p in paths]}


@server.tool(annotations=WRITES)
def remove_file(
    deposition_id: DepositionArg,
    filename: Annotated[str, Field(description="Name of the file to remove.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Remove one file from a draft by name."""
    try:
        with _client(sandbox) as client:
            removed = client.delete_file(deposition_id, filename)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc
    return {"id": deposition_id, "filename": filename, "removed": removed}


@server.tool(annotations=WRITES)
def publish_record(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
    confirm: ConfirmArg = None,
) -> dict[str, Any]:
    """Publish an existing draft. A published record can never be deleted."""
    _guard_production(sandbox, True, confirm)
    try:
        with _client(sandbox) as client:
            return _summarise(client.publish(deposition_id))
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=WRITES)
def submit_to_community(
    deposition_id: DepositionArg,
    community: Annotated[str, Field(description="Community slug to submit the draft to.")],
    comment: Annotated[str, Field(description="HTML note for the community curators.")] = "",
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Submit an existing draft to a community for inclusion.

    Attaches a community-submission review and submits it. A curator of the
    community must accept the request; acceptance is what publishes the record.
    While the request is open, Zenodo refuses to publish or delete the draft.
    """
    try:
        with _client(sandbox) as client:
            client.set_community_review(deposition_id, client.community_uuid(community))
            client.submit_review(deposition_id, comment=comment)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc
    return {"id": deposition_id, "community": community, "status": "submitted"}


@server.tool(annotations=WRITES)
def delete_draft(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
    confirm: Annotated[
        str | None,
        Field(description="Must be the literal 'DELETE' to delete on production zenodo.org."),
    ] = None,
) -> dict[str, Any]:
    """Delete a draft. A published record cannot be deleted."""
    _guard_production_delete(sandbox, confirm)
    try:
        with _client(sandbox) as client:
            client.delete_draft(deposition_id)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc
    return {"id": deposition_id, "status": "deleted"}


def main() -> None:  # pragma: no cover - process entry point
    """Run the server over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
