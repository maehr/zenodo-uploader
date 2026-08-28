"""MCP server exposing the Zenodo deposition lifecycle to any MCP host.

Runs over stdio. Every tool defaults to the sandbox, and anything irreversible
on production needs two independent confirmations; see :func:`_guard_production`.

Start it with::

    uvx --from zenodo-uploader zenodo-mcp
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx2
import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import operations
from .config import Settings, base_url_for
from .zenodo import ZenodoClient, ZenodoError

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


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Present every expected failure as a ToolError, never as a traceback.

    UsageError is a precondition the caller got wrong, ZenodoError is a failed
    request, and HTTPError is a transport failure such as a timeout. None of
    the three may escape to the host as an unhandled exception.
    """
    try:
        yield
    except (ValueError, AttributeError, ZenodoError, httpx2.HTTPError) as exc:
        # UsageError is a ValueError; so is a pydantic validation failure and a
        # missing required field. AttributeError covers malformed metadata.
        raise ToolError(str(exc)) from exc


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
    with _tool_errors(), _registry_client() as registry:
        return operations.preview(registry, doi, description=description, keep_doi=keep_doi)


@server.tool(annotations=READ_ONLY)
def check_doi(
    doi: Annotated[str, Field(description="DOI to look up on Zenodo.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Report whether a DOI is already on Zenodo, as a record or as your own draft.

    Use this before creating a record to avoid a duplicate.
    """
    with _tool_errors(), _client(sandbox) as client:
        return operations.check(client, doi)


@server.tool(annotations=READ_ONLY)
def get_deposition(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Fetch one deposition's current state, DOI, title, and link."""
    with _tool_errors(), _client(sandbox) as client:
        return operations.summarise(client.get_deposition(deposition_id))


@server.tool(annotations=READ_ONLY)
def list_files(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
) -> list[dict[str, Any]]:
    """List the files attached to a deposition."""
    with _tool_errors(), _client(sandbox) as client:
        return operations.list_files(client, deposition_id)


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
    keep_doi: Annotated[
        bool,
        Field(description="Keep the mirrored DOI (doi path only). False mints a new one."),
    ] = True,
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
    _guard_production(sandbox, publish, confirm)
    with _tool_errors():
        related_ids = operations.parse_related(related or {})
        paths = _paths(files)
        with _client(sandbox) as client, _registry_client() as registry:
            row = operations.create(
                client,
                registry,
                metadata=metadata,
                doi=doi,
                files=paths,
                community=community,
                description=description,
                related=related_ids,
                keep_doi=keep_doi,
                publish=publish,
            )
    # Report the same key every other tool reports.
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
    with _tool_errors(), _client(sandbox) as client:
        return operations.update(
            client,
            deposition_id,
            metadata,
            on_republish=lambda: _guard_production(sandbox, True, confirm),
        )


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
    with _tool_errors(), _client(sandbox) as client:
        return operations.new_version(
            client,
            deposition_id,
            files=_paths(files),
            metadata=metadata,
            publish=publish,
        )


@server.tool(annotations=WRITES)
def add_files(
    deposition_id: DepositionArg,
    files: Annotated[list[str], Field(description="Absolute paths of files to upload.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Add files to a deposition, overwriting a file of the same name."""
    with _tool_errors(), _client(sandbox) as client:
        return operations.add_files(client, deposition_id, _paths(files))


@server.tool(annotations=WRITES)
def remove_file(
    deposition_id: DepositionArg,
    filename: Annotated[str, Field(description="Name of the file to remove.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Remove one file from a draft by name."""
    with _tool_errors(), _client(sandbox) as client:
        row = operations.remove_files(client, deposition_id, [filename])
    return {"id": deposition_id, "filename": filename, "removed": bool(row["removed"])}


@server.tool(annotations=WRITES)
def publish_record(
    deposition_id: DepositionArg,
    sandbox: SandboxArg = True,
    confirm: ConfirmArg = None,
) -> dict[str, Any]:
    """Publish an existing draft. A published record can never be deleted."""
    _guard_production(sandbox, True, confirm)
    with _tool_errors(), _client(sandbox) as client:
        return operations.publish(client, deposition_id)


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
    with _tool_errors(), _client(sandbox) as client:
        return operations.submit_to_community(client, deposition_id, community, comment=comment)


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
    with _tool_errors(), _client(sandbox) as client:
        return operations.delete(client, deposition_id)


def main() -> None:  # pragma: no cover - process entry point
    """Run the server over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
