"""Resumable batch mirroring driven by a JSON manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from .mapping import work_to_zenodo
from .models import RelatedIdentifier
from .resolve import fetch_work
from .zenodo import ZenodoClient

log = structlog.get_logger()


class ManifestEntry(BaseModel):
    """One record to mirror: a DOI plus the files to attach.

    Examples:
        >>> ManifestEntry(doi="10.1/x", files=["a.pdf"]).doi
        '10.1/x'
    """

    doi: str
    files: list[Path] = Field(default_factory=list)
    description: str | None = None
    community: str | None = None
    related: list[RelatedIdentifier] = Field(default_factory=list)


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Load and validate a manifest file (a JSON array of entries)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ManifestEntry.model_validate(entry) for entry in raw]


def load_state(path: Path) -> dict[str, Any]:
    """Load the state file, returning an empty state when absent.

    Examples:
        >>> load_state(Path("/nonexistent/state.json"))
        {}
    """
    if not path.exists():
        return {}
    state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Persist the state file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def mirror_entry(
    client: ZenodoClient,
    datacite_client: Any,
    entry: ManifestEntry,
    *,
    publish: bool = False,
) -> dict[str, Any]:
    """Mirror a single manifest entry; returns a state row for the DOI.

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
    if existing := client.find_records_by_doi(entry.doi):
        url = existing[0].get("links", {}).get("html") or existing[0].get("links", {}).get(
            "self_html"
        )
        log.info("already published, skipping", doi=entry.doi, url=url)
        return _state_row("exists", record_url=url)
    if drafts := client.find_depositions_by_doi(entry.doi):
        deposition = drafts[0]
        log.info("reusing existing draft", doi=entry.doi, id=deposition["id"])
    else:
        record = fetch_work(datacite_client, entry.doi)
        metadata = work_to_zenodo(
            record,
            description=entry.description,
            extra_related=entry.related or None,
        )
        deposition = client.create_deposition(metadata)
        for path in entry.files:
            client.upload_file(deposition, path)
    dep_id = deposition["id"]
    if entry.community:
        client.set_community_review(dep_id, client.community_uuid(entry.community))
        if not publish:
            return _state_row("draft", deposition_id=dep_id, community=entry.community)
        client.submit_review(dep_id)
        return _state_row("submitted", deposition_id=dep_id, community=entry.community)
    if not publish:
        return _state_row("draft", deposition_id=dep_id)
    published = client.publish(dep_id)
    return _state_row(
        "published",
        deposition_id=dep_id,
        record_url=published.get("links", {}).get("html"),
    )


def run_batch(
    client: ZenodoClient,
    datacite_client: Any,
    entries: list[ManifestEntry],
    state_path: Path,
    *,
    publish: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Mirror all manifest entries, resuming from and updating the state file."""
    state = load_state(state_path)
    done = 0
    for entry in entries:
        row = state.get(entry.doi)
        if row and row.get("status") in ("published", "submitted", "exists"):
            continue
        if row and row.get("status") == "draft" and not publish:
            continue
        if limit is not None and done >= limit:
            break
        log.info("mirroring", doi=entry.doi)
        try:
            state[entry.doi] = mirror_entry(client, datacite_client, entry, publish=publish)
        except Exception as exc:
            state[entry.doi] = _state_row("error", error=str(exc))
            log.error("failed", doi=entry.doi, error=str(exc))
        save_state(state_path, state)
        done += 1
    return state


def _state_row(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "timestamp": datetime.now(UTC).isoformat(), **extra}
