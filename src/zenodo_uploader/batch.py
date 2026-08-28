"""Resumable batch mirroring driven by a JSON manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field, model_validator

from .mapping import work_to_zenodo
from .models import RelatedIdentifier, ZenodoMetadata, validate_deposit_metadata
from .resolve import fetch_work
from .zenodo import ZenodoClient

log = structlog.get_logger()


SOURCES = ("doi", "metadata", "metadata_file")


class ManifestEntry(BaseModel):
    """One record to create, from exactly one metadata source.

    A ``doi`` is resolved from DataCite or Crossref and mapped onto Zenodo
    fields. A ``metadata`` mapping, or a ``metadata_file`` holding one, is sent
    to Zenodo unchanged. Exactly one of the three must be present.

    Examples:
        >>> ManifestEntry(doi="10.1/x", files=["a.pdf"]).key
        '10.1/x'
        >>> ManifestEntry(id="poster", metadata={"title": "P"}).key
        'poster'
        >>> ManifestEntry(metadata_file="rec.json").key
        'rec.json'
        >>> ManifestEntry(doi="10.1/x", metadata={"title": "P"})
        Traceback (most recent call last):
        ...
        pydantic_core._pydantic_core.ValidationError: ...
    """

    id: str | None = None
    doi: str | None = None
    metadata: dict[str, Any] | None = None
    metadata_file: Path | None = None
    files: list[Path] = Field(default_factory=list)
    description: str | None = None
    community: str | None = None
    related: list[RelatedIdentifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ManifestEntry:
        """Reject an entry with no metadata source, or with more than one."""
        present = [name for name in SOURCES if getattr(self, name) is not None]
        if len(present) != 1:
            found = ", ".join(present) if present else "none"
            raise ValueError(f"give exactly one of {', '.join(SOURCES)}; found {found}")
        return self

    @property
    def key(self) -> str:
        """The state-file key: the explicit id, else the DOI, else the file path."""
        return self.id or self.doi or str(self.metadata_file)

    def resolve_metadata(self, base: Path | None = None) -> dict[str, Any] | None:
        """Return the literal metadata for this entry, or None for a DOI entry.

        A ``metadata_file`` path is read relative to ``base``, which lets a
        manifest name its records by a path relative to itself.
        """
        if self.metadata is not None:
            return dict(self.metadata)
        if self.metadata_file is None:
            return None
        path = self.metadata_file
        if base is not None and not path.is_absolute():
            path = base / path
        payload = json.loads(path.read_text(encoding="utf-8"))
        unwrapped: dict[str, Any] = payload.get("metadata", payload)
        return unwrapped


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Load and validate a manifest file (a JSON array of entries).

    Rejects a duplicate key before any network call, because two entries
    sharing a key would overwrite each other in the state file and one record
    would be created twice on every run.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = [ManifestEntry.model_validate(entry) for entry in raw]
    seen: set[str] = set()
    for entry in entries:
        if entry.key in seen:
            raise ValueError(f"duplicate manifest key: {entry.key!r}")
        seen.add(entry.key)
    return entries


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


def process_entry(
    client: ZenodoClient,
    datacite_client: Any,
    entry: ManifestEntry,
    *,
    publish: bool = False,
    base: Path | None = None,
) -> dict[str, Any]:
    """Create one record from a manifest entry; returns its state row.

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
    literal = entry.resolve_metadata(base)
    # Only a known DOI can be probed for. A literal-metadata entry carries one
    # only if its author put it there.
    probe_doi = entry.doi or (literal or {}).get("doi")
    if probe_doi:
        if existing := client.find_records_by_doi(probe_doi):
            url = existing[0].get("links", {}).get("html") or existing[0].get("links", {}).get(
                "self_html"
            )
            log.info("already published, skipping", doi=probe_doi, url=url)
            return _state_row("exists", record_url=url)
        drafts = client.find_depositions_by_doi(probe_doi)
    else:
        drafts = []
    if drafts:
        deposition = drafts[0]
        log.info("reusing existing draft", key=entry.key, id=deposition["id"])
    else:
        metadata: ZenodoMetadata | dict[str, Any]
        if literal is not None:
            validate_deposit_metadata(literal)
            metadata = literal
        else:
            assert entry.doi is not None  # guaranteed by the manifest validator
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
    base: Path | None = None,
) -> dict[str, Any]:
    """Create every manifest entry, resuming from and updating the state file.

    Each entry is keyed by :attr:`ManifestEntry.key`. A DOI entry is also
    checked against Zenodo itself, so it stays idempotent even without the
    state file. A literal-metadata entry has nothing to check against unless
    its metadata carries a DOI, so its only guard is the state file: delete the
    state file and rerun, and the record is created a second time.
    """
    state = load_state(state_path)
    done = 0
    for entry in entries:
        row = state.get(entry.key)
        if row and row.get("status") in ("published", "submitted", "exists"):
            continue
        if row and row.get("status") == "draft" and not publish:
            continue
        if limit is not None and done >= limit:
            break
        log.info("creating", key=entry.key)
        try:
            state[entry.key] = process_entry(
                client, datacite_client, entry, publish=publish, base=base
            )
        except Exception as exc:
            state[entry.key] = _state_row("error", error=str(exc))
            log.error("failed", key=entry.key, error=str(exc))
        save_state(state_path, state)
        done += 1
    return state


def _state_row(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "timestamp": datetime.now(UTC).isoformat(), **extra}
