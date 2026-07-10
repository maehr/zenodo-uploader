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
    """Mirror a single manifest entry; returns a state row for the DOI."""
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
            community=entry.community,
            description=entry.description,
            extra_related=entry.related or None,
        )
        deposition = client.create_deposition(metadata)
        for path in entry.files:
            client.upload_file(deposition, path)
    if not publish:
        return _state_row("draft", deposition_id=deposition["id"])
    published = client.publish(deposition["id"])
    return _state_row(
        "published",
        deposition_id=deposition["id"],
        record_url=published.get("links", {}).get("html"),
    )


def resync_entry(
    client: ZenodoClient,
    entry: ManifestEntry,
    *,
    submit: bool = False,
) -> dict[str, Any]:
    """Overwrite an unpublished draft's files with the manifest's files.

    Pushes updated (e.g. metadata-enhanced) files onto an existing draft: it
    fetches the full deposition (the search result lacks the bucket link),
    then re-uploads each file under the same key, which overwrites it in place.
    No file is deleted, no new version is created and the DOI is preserved;
    published records are left untouched.

    With ``submit=True``, community submission is opt-in: after overwriting the
    files it attaches and submits a community-submission review, using the
    manifest's community (falling back to any review already on the draft).
    Plain editable drafts — the common case — need none of that and are handled
    by the default path.
    """
    if client.find_records_by_doi(entry.doi):
        log.info("already published, skipping", doi=entry.doi)
        return _state_row("exists")
    drafts = client.find_depositions_by_doi(entry.doi)
    if not drafts:
        return _state_row("error", error="no draft to resync")
    dep_id = drafts[0]["id"]
    # The search result carries no links.bucket; the full deposition does.
    deposition = client.get_deposition(dep_id)
    for path in entry.files:
        client.upload_file(deposition, path)  # same-key PUT overwrites
    if not submit:
        return _state_row("resynced", deposition_id=dep_id)
    # Opt-in community submission: prefer the manifest's community, otherwise
    # fall back to the one a review is already attached to.
    community_uuid: str | None = None
    if entry.community:
        community_uuid = client.community_uuid(entry.community)
    elif review := client.get_review(dep_id):
        community_uuid = review.get("receiver", {}).get("community")
    if not community_uuid:
        return _state_row("error", error="cannot submit review without a community")
    client.set_community_review(dep_id, community_uuid)
    client.submit_review(dep_id)
    return _state_row("submitted", deposition_id=dep_id)


def run_resync(
    client: ZenodoClient,
    entries: list[ManifestEntry],
    state_path: Path,
    *,
    submit: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Resync every manifest entry's files, resuming from the state file."""
    state = load_state(state_path)
    done = 0
    for entry in entries:
        row = state.get(entry.doi)
        if row and row.get("status") in ("resynced", "submitted", "exists", "published"):
            continue
        if limit is not None and done >= limit:
            break
        log.info("resyncing", doi=entry.doi)
        try:
            state[entry.doi] = resync_entry(client, entry, submit=submit)
        except Exception as exc:
            state[entry.doi] = _state_row("error", error=str(exc))
            log.error("failed", doi=entry.doi, error=str(exc))
        save_state(state_path, state)
        done += 1
    return state


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
        if row and row.get("status") in ("published", "exists"):
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
