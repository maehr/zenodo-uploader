"""The Zenodo deposition lifecycle, with no interface of its own.

Every operation lives here once. The CLI and the MCP server are adapters: they
parse arguments, ask for confirmation, translate errors, and present results.
They must not carry lifecycle rules, because a rule that lives in two places
drifts apart.

An operation returns a plain dict with the keys a caller acts on, and raises
:class:`UsageError` when the caller asked for something Zenodo cannot do.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx2
import structlog

from .mapping import work_to_zenodo
from .models import RelatedIdentifier, ZenodoMetadata, validate_deposit_metadata
from .resolve import fetch_work
from .zenodo import ZenodoClient, is_zenodo_doi

log = structlog.get_logger()


class UsageError(ValueError):
    """The caller asked for something Zenodo will not do.

    Distinct from :class:`~zenodo_uploader.zenodo.ZenodoError`, which means a
    request failed. Each adapter turns this into its own kind of error.
    """


def summarise(deposition: Mapping[str, Any]) -> dict[str, Any]:
    """Trim a Zenodo deposition to the fields a caller acts on.

    Raw deposition payloads are large and mostly noise.

    Examples:
        >>> summarise({"id": 1, "state": "done", "links": {"html": "https://z/1"},
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


def _row(status: str, **extra: Any) -> dict[str, Any]:
    """Build a result row, dropping the keys that have no value."""
    return {"status": status, **{k: v for k, v in extra.items() if v is not None}}


def unwrap(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Accept both metadata shapes and validate the result.

    A ``.zenodo.json`` puts the fields at the top level; the API wraps them in
    ``{"metadata": {...}}``. Both are common, so both are accepted.

    Examples:
        >>> unwrap({"metadata": {"title": "T"}})
        {'title': 'T'}
        >>> unwrap({"title": "T"})
        {'title': 'T'}
    """
    inner = metadata.get("metadata", metadata)
    return dict(inner)


def read_metadata_file(path: Path) -> dict[str, Any]:
    """Read a Zenodo metadata file and check the fields Zenodo requires."""
    import json

    meta = unwrap(json.loads(path.read_text(encoding="utf-8")))
    validate_deposit_metadata(meta)
    return meta


def parse_related(pairs: Mapping[str, str]) -> list[RelatedIdentifier]:
    """Turn {relation: identifier} into related identifiers, or explain why not.

    Examples:
        >>> parse_related({"isPartOf": "10.1/x"})[0].relation
        'isPartOf'
    """
    try:
        return [
            RelatedIdentifier.model_validate({"relation": k, "identifier": v})
            for k, v in pairs.items()
        ]
    except ValueError as exc:
        raise UsageError(f"invalid related identifier: {exc}") from exc


def _resolve(registry: httpx2.Client, doi: str) -> Any:
    """Fetch DOI metadata, reporting any failure the same way everywhere."""
    try:
        return fetch_work(registry, doi)
    except httpx2.HTTPError as exc:
        # HTTPError covers both a bad status and a transport failure such as a
        # timeout, so neither escapes as an unhandled exception.
        raise UsageError(f"cannot resolve {doi}: {exc}") from exc


def preview(
    registry: httpx2.Client,
    doi: str,
    *,
    description: str | None = None,
    keep_doi: bool = True,
) -> dict[str, Any]:
    """Resolve a DOI and return the Zenodo metadata it maps to. Writes nothing."""
    record = _resolve(registry, doi)
    return work_to_zenodo(record, description=description, keep_doi=keep_doi).to_payload()[
        "metadata"
    ]


def create(
    client: ZenodoClient,
    registry: httpx2.Client,
    *,
    metadata: Mapping[str, Any] | None = None,
    doi: str | None = None,
    files: Iterable[Path] = (),
    community: str | None = None,
    description: str | None = None,
    related: Iterable[RelatedIdentifier] = (),
    keep_doi: bool = True,
    publish: bool = False,
) -> dict[str, Any]:
    """Create a deposition from literal metadata, or by mirroring a DOI.

    Give exactly one of ``metadata`` or ``doi``.

    A community is not metadata. Zenodo accepts and echoes back the legacy
    ``communities`` deposit field but never acts on it, so this attaches a
    community-submission review to the draft instead. Four outcomes:

    ==========  =======  ==========================================  ===========
    community   publish  action                                      status
    ==========  =======  ==========================================  ===========
    no          no       stop at the draft                           ``draft``
    no          yes      publish the draft                           ``published``
    yes         no       attach the review, leave it unsubmitted     ``draft``
    yes         yes      attach and submit the review                ``submitted``
    ==========  =======  ==========================================  ===========

    A submitted review is not a published record: it is an inclusion request
    that a curator of the community must accept. Acceptance publishes the
    record. Until then the draft stays private, and Zenodo refuses both a
    direct publish and a delete while the request is open.
    """
    if (metadata is None) == (doi is None):
        raise UsageError("give exactly one of metadata or doi")

    literal = unwrap(metadata) if metadata is not None else None
    if literal is not None:
        validate_deposit_metadata(literal)

    # Only a known DOI can be probed for. A literal-metadata record carries one
    # only if its author put it there.
    probe_doi = doi or (literal or {}).get("doi")
    deposition = None
    if probe_doi:
        if existing := client.find_records_by_doi(probe_doi):
            links = existing[0].get("links", {})
            url = links.get("html") or links.get("self_html")
            log.info("already published, skipping", doi=probe_doi, url=url)
            return _row("exists", record_url=url)
        if drafts := client.find_depositions_by_doi(probe_doi):
            deposition = drafts[0]
            log.info("reusing existing draft", doi=probe_doi, id=deposition["id"])

    if deposition is None:
        payload: ZenodoMetadata | dict[str, Any]
        if literal is not None:
            payload = literal
        else:
            assert doi is not None  # guaranteed by the check above
            payload = work_to_zenodo(
                _resolve(registry, doi),
                description=description,
                extra_related=list(related) or None,
                keep_doi=keep_doi,
            )
        deposition = client.create_deposition(payload)
        for path in files:
            client.upload_file(deposition, path)

    dep_id = deposition["id"]
    if community:
        client.set_community_review(dep_id, client.community_uuid(community))
        if not publish:
            return _row("draft", deposition_id=dep_id, community=community)
        client.submit_review(dep_id)
        return _row("submitted", deposition_id=dep_id, community=community)
    if not publish:
        return _row("draft", deposition_id=dep_id)
    published = client.publish(dep_id)
    return _row(
        "published", deposition_id=dep_id, record_url=published.get("links", {}).get("html")
    )


def update(
    client: ZenodoClient,
    deposition_id: int,
    metadata: Mapping[str, Any],
    *,
    on_republish: Any = None,
) -> dict[str, Any]:
    """Replace the metadata of a deposition.

    On a draft this is a plain update. On a published record it unlocks the
    record, updates it, and publishes it again.

    A published record whose DOI Zenodo minted cannot be published a second
    time: Zenodo rejects it with a ``pids.doi`` validation error. This raises
    :class:`UsageError` for that case before it changes anything, and names
    :func:`new_version` instead.

    ``on_republish`` is called with no arguments just before the irreversible
    step, so an adapter can ask for confirmation at the right moment.
    """
    meta = unwrap(metadata)
    validate_deposit_metadata(meta)

    current = client.get_deposition(deposition_id)
    published = current.get("state") in ("done", "inprogress")
    if published and is_zenodo_doi(current.get("doi")):
        raise UsageError(
            f"record {deposition_id} is published with a Zenodo-minted DOI "
            f"({current.get('doi')}), which Zenodo refuses to publish again. "
            f"Create a new version of {deposition_id} instead."
        )
    if not published:
        client.update_deposition(deposition_id, meta)
        return summarise(client.get_deposition(deposition_id)) | {"status": "draft updated"}

    if on_republish is not None:
        on_republish()
    client.edit_deposition(deposition_id)
    try:
        client.update_deposition(deposition_id, meta)
        record = client.publish(deposition_id)
    except Exception:
        # Never leave the record stuck in an open edit session.
        client.discard_edit(deposition_id)
        raise
    return summarise(record) | {"status": "republished"}


def new_version(
    client: ZenodoClient,
    deposition_id: int,
    *,
    files: Iterable[Path] = (),
    metadata: Mapping[str, Any] | None = None,
    publish: bool = False,
) -> dict[str, Any]:
    """Open a new version of a published record, and optionally publish it.

    The new draft inherits the files of the previous version and keeps the
    concept DOI, so the record stays one citable series.

    Zenodo versions a record through its concept DOI, and creates one only for
    a DOI it minted itself. A record that kept an external DOI has none, so
    this raises :class:`UsageError` and names :func:`update` instead.

    Caution: Zenodo allows only one unpublished new version per record, so a
    draft left behind by a failure blocks every later attempt. Everything that
    can fail is therefore checked before the draft is opened, and the draft is
    deleted again if a later step fails.
    """
    # Validate before opening anything: a rejected payload must not leave a
    # draft behind that blocks the next attempt.
    meta = unwrap(metadata) if metadata is not None else None
    if meta is not None:
        validate_deposit_metadata(meta)
    paths = list(files)

    current = client.get_deposition(deposition_id)
    if not current.get("conceptdoi"):
        raise UsageError(
            f"record {deposition_id} has no concept DOI, because its DOI "
            f"({current.get('doi')}) is not one Zenodo minted. Zenodo cannot version it. "
            f"Update the metadata of {deposition_id} instead."
        )

    draft = client.new_version(deposition_id)
    draft_id = int(draft["id"])
    try:
        if meta is not None:
            client.update_deposition(draft_id, meta)
        for path in paths:
            client.upload_file(draft, path)
        if publish:
            return summarise(client.publish(draft_id)) | {
                "status": "published",
                "of": deposition_id,
            }
        return summarise(client.get_deposition(draft_id)) | {
            "status": "draft",
            "of": deposition_id,
        }
    except Exception:
        # An abandoned new-version draft blocks every later new version, so
        # clean it up rather than leave the record wedged.
        try:
            client.delete_draft(draft_id)
            log.info("rolled back the new-version draft", id=draft_id)
        except Exception:  # pragma: no cover - best effort, original error wins
            log.error("could not roll back the new-version draft", id=draft_id)
        raise


def publish(client: ZenodoClient, deposition_id: int) -> dict[str, Any]:
    """Publish an existing draft. A published record can never be deleted."""
    return summarise(client.publish(deposition_id))


def submit_to_community(
    client: ZenodoClient, deposition_id: int, community: str, *, comment: str = ""
) -> dict[str, Any]:
    """Submit a draft to a community for inclusion.

    A curator must accept the request, and that acceptance is what publishes
    the record. While the request is open, Zenodo refuses to publish the draft
    and refuses to delete it.
    """
    client.set_community_review(deposition_id, client.community_uuid(community))
    client.submit_review(deposition_id, comment=comment)
    return {"id": deposition_id, "community": community, "status": "submitted"}


def delete(client: ZenodoClient, deposition_id: int) -> dict[str, Any]:
    """Delete a draft. A published record cannot be deleted."""
    client.delete_draft(deposition_id)
    return {"id": deposition_id, "status": "deleted"}


def list_files(client: ZenodoClient, deposition_id: int) -> list[dict[str, Any]]:
    """List the files of a deposition, as name and size."""
    return [
        {"filename": f.get("filename"), "size": f.get("filesize")}
        for f in client.list_files(deposition_id)
    ]


def add_files(client: ZenodoClient, deposition_id: int, files: Iterable[Path]) -> dict[str, Any]:
    """Add files to a deposition, overwriting a file of the same name."""
    paths = list(files)
    deposition = client.get_deposition(deposition_id)
    for path in paths:
        client.upload_file(deposition, path)
    return {"id": deposition_id, "added": [p.name for p in paths]}


def remove_files(
    client: ZenodoClient, deposition_id: int, filenames: Iterable[str]
) -> dict[str, Any]:
    """Remove files from a draft by name, reporting which were absent."""
    names = list(filenames)
    removed = [name for name in names if client.delete_file(deposition_id, name)]
    return {
        "id": deposition_id,
        "removed": removed,
        "absent": [n for n in names if n not in removed],
    }


def check(client: ZenodoClient, doi: str) -> dict[str, Any]:
    """Report whether a DOI is already on Zenodo, published or as your draft."""
    records = client.find_records_by_doi(doi)
    drafts = client.find_depositions_by_doi(doi)
    return {
        "doi": doi,
        "published": [r.get("links", {}).get("html") for r in records],
        "drafts": [d.get("id") for d in drafts],
    }
