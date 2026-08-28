"""MCP server exposing the Zenodo uploader to any MCP host.

Runs over stdio. Every tool defaults to the sandbox, and publishing to
production needs two independent confirmations; see :func:`_guard_production`.

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

from .batch import ManifestEntry, mirror_entry
from .config import Settings, base_url_for
from .mapping import work_to_zenodo
from .models import RelatedIdentifier, validate_deposit_metadata
from .resolve import fetch_work
from .zenodo import ZenodoClient, ZenodoError

# stdio is the transport: anything on stdout corrupts the protocol stream.
structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))

server = MCPServer(
    name="zenodo",
    instructions=(
        "Mirror DOIs and upload records to Zenodo. Resolve a DOI with preview_doi "
        "before any write. Every tool works against sandbox.zenodo.org unless "
        "sandbox=false. Publishing on production is permanent and needs both the "
        "ZENODO_ALLOW_PRODUCTION_PUBLISH=1 environment variable and confirm='PUBLISH'."
    ),
)

ALLOW_PRODUCTION_PUBLISH_ENV = "ZENODO_ALLOW_PRODUCTION_PUBLISH"

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

SandboxArg = Annotated[
    bool,
    Field(description="Use sandbox.zenodo.org. Set false only for the real zenodo.org."),
]
ConfirmArg = Annotated[
    str | None,
    Field(description="Must be the literal 'PUBLISH' to publish on production zenodo.org."),
]


def _guard_production(sandbox: bool, publish: bool, confirm: str | None) -> None:
    """Refuse a production publish that is not doubly confirmed.

    A CLI can ask a human at the terminal. An MCP server has no terminal, so
    the operator opts in once through the environment and the caller confirms
    again per call. Publishing on zenodo.org cannot be undone.

    Examples:
        >>> _guard_production(sandbox=True, publish=True, confirm=None)
        >>> _guard_production(sandbox=False, publish=False, confirm=None)
    """
    if sandbox or not publish:
        return
    if os.environ.get(ALLOW_PRODUCTION_PUBLISH_ENV) != "1":
        raise ToolError(
            "Refusing to publish on production zenodo.org: the server was not started "
            f"with {ALLOW_PRODUCTION_PUBLISH_ENV}=1. Publish on the sandbox instead "
            "(sandbox=true), or ask the operator to restart the server with that "
            "variable set."
        )
    if confirm != "PUBLISH":
        raise ToolError(
            "Refusing to publish on production zenodo.org: pass confirm='PUBLISH'. "
            "Published records can never be deleted."
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

    Metadata comes from DataCite, falling back to Crossref. Run this before any
    write tool so the caller can check the mapping first.
    """
    try:
        with _registry_client() as registry:
            record = fetch_work(registry, doi)
    except httpx2.HTTPStatusError as exc:
        raise ToolError(f"cannot resolve {doi}: {exc.response.status_code}") from exc
    metadata = work_to_zenodo(record, description=description, keep_doi=keep_doi)
    return metadata.to_payload()["metadata"]


@server.tool(annotations=READ_ONLY)
def check_doi(
    doi: Annotated[str, Field(description="DOI to look up on Zenodo.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Report whether a DOI is already on Zenodo, as a record or as your own draft.

    Use this before mirroring to avoid creating a duplicate.
    """
    with _client(sandbox) as client:
        records = client.find_records_by_doi(doi)
        drafts = client.find_depositions_by_doi(doi)
    return {
        "doi": doi,
        "published": [r.get("links", {}).get("html") for r in records],
        "drafts": [d.get("id") for d in drafts],
    }


@server.tool(annotations=WRITES)
def mirror_doi(
    doi: Annotated[str, Field(description="DOI to mirror onto Zenodo.")],
    files: Annotated[
        list[str] | None, Field(description="Absolute paths of files to attach.")
    ] = None,
    community: Annotated[
        str | None,
        Field(description="Community slug. Attaches an inclusion request, not metadata."),
    ] = None,
    description: Annotated[
        str | None, Field(description="Override the synthesized HTML description.")
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
    """Mirror a DOI to Zenodo: registry metadata in, Zenodo deposition out.

    The existing DOI is kept, so the mirror stays citable under its canonical
    identifier. An already-mirrored DOI is reported, never duplicated.

    A community is not metadata. With `community`, this attaches a
    community-submission review; with `publish` as well, it submits that review
    as an inclusion request that a curator must accept. The returned status is
    then `submitted`, not `published` — the record is not public yet.
    """
    _guard_production(sandbox, publish, confirm)
    try:
        entry = ManifestEntry(
            doi=doi,
            files=[Path(f) for f in files or []],
            description=description,
            community=community,
            related=[
                RelatedIdentifier.model_validate({"relation": k, "identifier": v})
                for k, v in (related or {}).items()
            ],
        )
    except ValueError as exc:
        raise ToolError(f"invalid arguments: {exc}") from exc
    for path in entry.files:
        if not path.is_file():
            raise ToolError(f"file not found: {path}")
    try:
        with _client(sandbox) as client, _registry_client() as registry:
            return mirror_entry(client, registry, entry, publish=publish)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=WRITES)
def upload_record(
    metadata: Annotated[
        dict[str, Any],
        Field(description="Zenodo deposit metadata, as in a .zenodo.json file."),
    ],
    files: Annotated[
        list[str] | None, Field(description="Absolute paths of files to attach.")
    ] = None,
    sandbox: SandboxArg = True,
    publish: Annotated[bool, Field(description="Publish instead of stopping at a draft.")] = False,
    confirm: ConfirmArg = None,
) -> dict[str, Any]:
    """Create a deposition from raw Zenodo metadata, sent verbatim.

    Accepts a `.zenodo.json` shape, with the fields at the top level or wrapped
    in `{"metadata": {...}}`. Omit `doi` to have Zenodo mint one, or include it
    to keep an existing DOI. `title`, `upload_type`, `description`,
    `publication_date`, and a `name` per creator are required.
    """
    _guard_production(sandbox, publish, confirm)
    meta = metadata.get("metadata", metadata)
    try:
        validate_deposit_metadata(meta)
    except (ValueError, AttributeError) as exc:
        raise ToolError(str(exc)) from exc
    paths = [Path(f) for f in files or []]
    for path in paths:
        if not path.is_file():
            raise ToolError(f"file not found: {path}")
    try:
        with _client(sandbox) as client:
            deposition = client.create_deposition(meta)
            for path in paths:
                client.upload_file(deposition, path)
            if publish:
                deposition = client.publish(deposition["id"])
            return _summarise(deposition)
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=WRITES)
def submit_to_community(
    deposition_id: Annotated[int, Field(description="Deposition (record) id of the draft.")],
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


@server.tool(annotations=READ_ONLY)
def get_deposition(
    deposition_id: Annotated[int, Field(description="Deposition id to fetch.")],
    sandbox: SandboxArg = True,
) -> dict[str, Any]:
    """Fetch one deposition's current state, DOI, title, and link."""
    try:
        with _client(sandbox) as client:
            return _summarise(client.get_deposition(deposition_id))
    except ZenodoError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:  # pragma: no cover - process entry point
    """Run the server over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
