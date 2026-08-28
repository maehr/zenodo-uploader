"""Thin client for the Zenodo legacy deposit REST API."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx2
import structlog

from .models import ZenodoMetadata

log = structlog.get_logger()

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
MIN_REQUEST_INTERVAL = 1.0  # Zenodo allows ~100 requests/minute.

# Prefixes Zenodo mints and manages itself: production and sandbox.
ZENODO_DOI_PREFIXES = ("10.5281/", "10.5072/")


def is_zenodo_doi(doi: str | None) -> bool:
    """Report whether Zenodo minted and manages this DOI.

    The answer decides whether a published record can be edited and published
    again. See :meth:`ZenodoClient.edit_deposition`.

    Examples:
        >>> is_zenodo_doi("10.5281/zenodo.123")
        True
        >>> is_zenodo_doi("10.30965/9783657796823")
        False
        >>> is_zenodo_doi(None)
        False
    """
    return bool(doi) and str(doi).startswith(ZENODO_DOI_PREFIXES)


class ZenodoError(RuntimeError):
    """A Zenodo API call failed after retries."""


class ZenodoClient:
    """Synchronous client for depositions on zenodo.org or sandbox.zenodo.org."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._last_request = 0.0
        self._client = httpx2.Client(
            headers={"Authorization": f"Bearer {token}"},
            # Generous read/write timeouts: a bucket upload of a large file
            # (100 MB and up) overruns a flat 60 s timeout.
            timeout=httpx2.Timeout(60.0, read=300.0, write=600.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ZenodoClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        """Issue a throttled request, retrying on 429 and transient 5xx."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            response = self._client.request(method, url, **kwargs)
            if response.status_code not in RETRY_STATUS:
                return response
            retry_after = float(response.headers.get("Retry-After", 2**attempt))
            log.warning("retrying", url=url, status=response.status_code, wait=retry_after)
            self._sleep(retry_after)
        raise ZenodoError(f"{method} {url} failed after {MAX_ATTEMPTS} attempts")

    def _json_or_raise(self, response: httpx2.Response) -> Any:
        if response.status_code >= 400:
            raise ZenodoError(
                f"{response.request.method} {response.request.url} -> "
                f"{response.status_code}: {response.text[:2000]}"
            )
        return response.json()

    def find_records_by_doi(self, doi: str) -> list[dict[str, Any]]:
        """Search published records for a DOI (idempotency probe)."""
        response = self._request(
            "GET", f"{self.base_url}/api/records", params={"q": f'doi:"{doi}"'}
        )
        payload = self._json_or_raise(response)
        hits: list[dict[str, Any]] = payload.get("hits", {}).get("hits", [])
        return [h for h in hits if h.get("doi") == doi or h.get("metadata", {}).get("doi") == doi]

    def find_depositions_by_doi(self, doi: str) -> list[dict[str, Any]]:
        """Search the token owner's depositions (drafts included) for a DOI."""
        response = self._request(
            "GET",
            f"{self.base_url}/api/deposit/depositions",
            params={"q": f'doi:"{doi}"', "all_versions": "true"},
        )
        payload = self._json_or_raise(response)
        return [d for d in payload if d.get("metadata", {}).get("doi") == doi]

    def create_deposition(self, metadata: ZenodoMetadata | Mapping[str, Any]) -> dict[str, Any]:
        """Create a new draft deposition.

        Accepts a :class:`ZenodoMetadata` (from the DataCite mapping path) or an
        already-unwrapped metadata mapping (from a ``.zenodo.json`` file), whose
        fields are sent to Zenodo verbatim.
        """
        payload = (
            metadata.to_payload()
            if isinstance(metadata, ZenodoMetadata)
            else {"metadata": dict(metadata)}
        )
        response = self._request("POST", f"{self.base_url}/api/deposit/depositions", json=payload)
        deposition: dict[str, Any] = self._json_or_raise(response)
        log.info("deposition created", id=deposition.get("id"))
        return deposition

    def get_deposition(self, deposition_id: int) -> dict[str, Any]:
        """Fetch a single deposition (draft or published)."""
        response = self._request("GET", f"{self.base_url}/api/deposit/depositions/{deposition_id}")
        deposition: dict[str, Any] = self._json_or_raise(response)
        return deposition

    def update_deposition(
        self, deposition_id: int, metadata: ZenodoMetadata | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Replace a draft's metadata (PUT); files are left untouched."""
        payload = (
            metadata.to_payload()
            if isinstance(metadata, ZenodoMetadata)
            else {"metadata": dict(metadata)}
        )
        response = self._request(
            "PUT", f"{self.base_url}/api/deposit/depositions/{deposition_id}", json=payload
        )
        deposition: dict[str, Any] = self._json_or_raise(response)
        log.info("deposition updated", id=deposition_id)
        return deposition

    def community_uuid(self, slug: str) -> str:
        """Resolve a community slug to its InvenioRDM UUID."""
        response = self._request("GET", f"{self.base_url}/api/communities/{slug}")
        community: dict[str, Any] = self._json_or_raise(response)
        return str(community["id"])

    def set_community_review(self, record_id: int, community_uuid: str) -> dict[str, Any]:
        """Attach a pending community-submission review to a draft (no submit).

        On current (InvenioRDM) Zenodo the legacy ``communities`` metadata field
        is ignored; a record joins a community through a review request. This
        creates (but does not submit) that request, leaving the draft private.
        """
        review: dict[str, Any] = self._json_or_raise(
            self._request(
                "PUT",
                f"{self.base_url}/api/records/{record_id}/draft/review",
                json={
                    "receiver": {"community": community_uuid},
                    "type": "community-submission",
                },
            )
        )
        log.info("community review attached", id=record_id, community=community_uuid)
        return review

    def submit_review(self, record_id: int, comment: str = "") -> dict[str, Any]:
        """Submit a draft's attached community review for curator acceptance.

        This is the publish step: the record becomes a submission awaiting the
        community's acceptance. Requires :meth:`set_community_review` first.
        """
        body = {"payload": {"content": comment, "format": "html"}} if comment else {}
        review: dict[str, Any] = self._json_or_raise(
            self._request(
                "POST",
                f"{self.base_url}/api/records/{record_id}/draft/actions/submit-review",
                json=body,
            )
        )
        log.info("submitted for community review", id=record_id)
        return review

    def upload_file(self, deposition: dict[str, Any], path: Path) -> dict[str, Any]:
        """Upload a local file into the deposition's bucket."""
        bucket_url = deposition["links"]["bucket"]
        with path.open("rb") as handle:
            response = self._request("PUT", f"{bucket_url}/{path.name}", content=handle.read())
        result: dict[str, Any] = self._json_or_raise(response)
        log.info("file uploaded", file=path.name, size=result.get("size"))
        return result

    def edit_deposition(self, deposition_id: int) -> dict[str, Any]:
        """Unlock a published record so its metadata can be changed.

        Moves the deposition from ``done`` to ``inprogress``. Calling it on a
        record that is already being edited succeeds again, despite what the
        API reference says, so no guard is needed.

        Caution: a record whose DOI Zenodo minted cannot be published again
        after an edit. Zenodo rejects the re-publish with a ``pids.doi``
        validation error, because the legacy API resubmits a prefix that Zenodo
        manages. Only a record with an external DOI survives the round trip.
        Use :meth:`new_version` for the rest. Check with :func:`is_zenodo_doi`
        before you start.
        """
        response = self._request(
            "POST", f"{self.base_url}/api/deposit/depositions/{deposition_id}/actions/edit"
        )
        deposition: dict[str, Any] = self._json_or_raise(response)
        log.info("deposition unlocked for editing", id=deposition_id)
        return deposition

    def discard_edit(self, deposition_id: int) -> None:
        """Abandon an open edit session and restore the published metadata."""
        response = self._request(
            "POST", f"{self.base_url}/api/deposit/depositions/{deposition_id}/actions/discard"
        )
        if response.status_code not in (200, 201, 204):
            raise ZenodoError(
                f"discarding the edit of {deposition_id} failed: {response.status_code}"
            )
        log.info("edit discarded", id=deposition_id)

    def new_version(self, deposition_id: int) -> dict[str, Any]:
        """Open a new version of a published record and return its draft.

        Zenodo returns the new draft directly, not the record the call was made
        against, so the result is the deposition to add files to and publish.
        The draft inherits the files of the previous version, and the concept
        DOI stays the same across versions.

        Only one unpublished new version can exist at a time.
        """
        response = self._request(
            "POST", f"{self.base_url}/api/deposit/depositions/{deposition_id}/actions/newversion"
        )
        draft: dict[str, Any] = self._json_or_raise(response)
        log.info("new version opened", of=deposition_id, draft=draft.get("id"))
        return draft

    def list_files(self, deposition_id: int) -> list[dict[str, Any]]:
        """List the files of a deposition, newest metadata first."""
        response = self._request(
            "GET", f"{self.base_url}/api/deposit/depositions/{deposition_id}/files"
        )
        files: list[dict[str, Any]] = self._json_or_raise(response)
        return files

    def delete_file(self, deposition_id: int, filename: str) -> bool:
        """Delete one file from a draft by name. Returns False if it is absent.

        The file has to be found by name first, because deletion works through
        the file id. The bucket route answers 404 for a file that is present,
        so it cannot be used here.
        """
        match = next(
            (f for f in self.list_files(deposition_id) if f.get("filename") == filename), None
        )
        if match is None:
            log.info("file already absent", id=deposition_id, file=filename)
            return False
        response = self._request(
            "DELETE",
            f"{self.base_url}/api/deposit/depositions/{deposition_id}/files/{match['id']}",
        )
        if response.status_code not in (200, 204, 404):
            raise ZenodoError(
                f"deleting file {filename!r} from {deposition_id} failed: {response.status_code}"
            )
        log.info("file deleted", id=deposition_id, file=filename)
        return True

    def publish(self, deposition_id: int) -> dict[str, Any]:
        """Publish a draft deposition. Published records cannot be deleted."""
        response = self._request(
            "POST",
            f"{self.base_url}/api/deposit/depositions/{deposition_id}/actions/publish",
        )
        record: dict[str, Any] = self._json_or_raise(response)
        log.info("published", id=deposition_id, url=record.get("links", {}).get("html"))
        return record

    def delete_draft(self, deposition_id: int) -> None:
        """Delete an unpublished draft deposition."""
        response = self._request(
            "DELETE", f"{self.base_url}/api/deposit/depositions/{deposition_id}"
        )
        if response.status_code not in (201, 204):
            raise ZenodoError(f"deleting draft {deposition_id} failed: {response.status_code}")
