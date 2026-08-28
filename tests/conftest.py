"""Shared fixtures: a fake in-memory Zenodo served via httpx2.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from zenodo_uploader.zenodo import ZenodoClient

DATACITE_CHAPTER: dict[str, Any] = {
    "doi": "10.5555/example-chapter",
    "titles": [{"title": "A Chapter About Something"}],
    "creators": [
        {
            "name": "Doe, Jane",
            "givenName": "Jane",
            "familyName": "Doe",
            "nameIdentifiers": [
                {
                    "nameIdentifierScheme": "ORCID",
                    "nameIdentifier": "https://orcid.org/0000-0002-1825-0097",
                }
            ],
        }
    ],
    "publisher": "Example Press",
    "publicationYear": 2024,
    "types": {"resourceTypeGeneral": "BookChapter"},
    "rightsList": [{"rightsIdentifier": "cc-by-nc-4.0"}],
    "url": "https://example.org/books/1/chapters/3",
    "language": "de",
    "descriptions": [],
    "relatedIdentifiers": [
        {
            "relationType": "IsPartOf",
            "relatedIdentifier": "https://doi.org/10.5555/example-book",
            "relatedIdentifierType": "DOI",
        }
    ],
}


def datacite_handler(request: httpx2.Request) -> httpx2.Response:
    """Answer every DataCite lookup with the sample chapter."""
    return httpx2.Response(200, request=request, json={"data": {"attributes": DATACITE_CHAPTER}})


class FakeZenodo:
    """Minimal stateful double of the Zenodo deposit API."""

    def __init__(self) -> None:
        self.depositions: dict[int, dict[str, Any]] = {}
        self.files: dict[int, list[str]] = {}
        self.published: set[int] = set()
        self.reviews: dict[int, dict[str, Any]] = {}
        self.submitted: set[int] = set()
        self.next_id = 100
        self.fail_next: int | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx2.Response(status, request=request, json={"message": "try later"})
        path = request.url.path
        if path == "/api/records":
            return self._search_records(request)
        if path == "/api/deposit/depositions" and request.method == "GET":
            return self._search_depositions(request)
        if path == "/api/deposit/depositions" and request.method == "POST":
            return self._create(request)
        if path.startswith("/bucket/"):
            return self._upload(request)
        if path.endswith("/actions/publish"):
            return self._publish(request)
        if path.endswith("/actions/edit"):
            return self._edit(request)
        if path.endswith("/actions/discard"):
            return self._discard(request)
        if path.endswith("/actions/newversion"):
            return self._new_version(request)
        if path.endswith("/files") and request.method == "GET":
            return self._list_files(request)
        if "/files/" in path and request.method == "DELETE":
            return self._delete_file(request)
        if path.endswith("/draft/review"):
            return self._set_review(request)
        if path.endswith("/draft/actions/submit-review"):
            return self._submit_review(request)
        if path.startswith("/api/communities/"):
            return httpx2.Response(200, request=request, json={"id": "uuid-of-community"})
        if path.startswith("/api/deposit/depositions/") and request.method == "GET":
            dep_id = int(path.rsplit("/", 1)[-1])
            return httpx2.Response(200, request=request, json=self.depositions[dep_id])
        if path.startswith("/api/deposit/depositions/") and request.method == "PUT":
            return self._update(request)
        if path.startswith("/api/deposit/depositions/") and request.method == "DELETE":
            return self._delete(request)
        return httpx2.Response(404, request=request, json={})

    def _edit(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.split("/")[4])
        if dep_id not in self.published:
            return httpx2.Response(400, request=request, json={"message": "not published"})
        self.depositions[dep_id]["state"] = "inprogress"
        return httpx2.Response(201, request=request, json=self.depositions[dep_id])

    def _discard(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.split("/")[4])
        self.depositions[dep_id]["state"] = "done"
        return httpx2.Response(204, request=request)

    def _new_version(self, request: httpx2.Request) -> httpx2.Response:
        """Zenodo returns the NEW draft here, not the record acted on."""
        dep_id = int(request.url.path.split("/")[4])
        if dep_id not in self.published:
            return httpx2.Response(400, request=request, json={"message": "not published"})
        new_id = self.next_id
        self.next_id += 1
        draft = {
            "id": new_id,
            "state": "unsubmitted",
            "metadata": dict(self.depositions[dep_id]["metadata"]),
            "links": {
                "bucket": f"https://zenodo.example/bucket/{new_id}",
                "html": f"https://zenodo.example/deposit/{new_id}",
                "latest_draft": f"https://zenodo.example/api/deposit/depositions/{new_id}",
            },
        }
        self.depositions[new_id] = draft
        # A new version inherits the files of the version it came from.
        self.files[new_id] = list(self.files.get(dep_id, []))
        return httpx2.Response(201, request=request, json=draft)

    def _list_files(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.split("/")[4])
        return httpx2.Response(
            200,
            request=request,
            json=[{"id": f"fid-{n}", "filename": n} for n in self.files.get(dep_id, [])],
        )

    def _delete_file(self, request: httpx2.Request) -> httpx2.Response:
        parts = request.url.path.split("/")
        dep_id, file_id = int(parts[4]), parts[6]
        name = file_id.removeprefix("fid-")
        files = self.files.get(dep_id, [])
        if name not in files:
            return httpx2.Response(404, request=request, json={})
        files.remove(name)
        return httpx2.Response(204, request=request)

    def _update(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.rsplit("/", 1)[-1])
        self.depositions[dep_id]["metadata"] = json.loads(request.content)["metadata"]
        return httpx2.Response(200, request=request, json=self.depositions[dep_id])

    def _set_review(self, request: httpx2.Request) -> httpx2.Response:
        record_id = int(request.url.path.split("/")[3])
        self.reviews[record_id] = json.loads(request.content)
        return httpx2.Response(201, request=request, json={"id": "review-1"})

    def _submit_review(self, request: httpx2.Request) -> httpx2.Response:
        record_id = int(request.url.path.split("/")[3])
        self.submitted.add(record_id)
        return httpx2.Response(200, request=request, json={"status": "submitted"})

    def _search_records(self, request: httpx2.Request) -> httpx2.Response:
        doi = request.url.params["q"].removeprefix('doi:"').removesuffix('"')
        hits = [
            {"doi": doi, "links": {"html": f"https://zenodo.example/records/{dep_id}"}}
            for dep_id in self.published
            if self.depositions[dep_id]["metadata"].get("doi") == doi
        ]
        return httpx2.Response(
            200, request=request, json={"hits": {"hits": hits, "total": len(hits)}}
        )

    def _search_depositions(self, request: httpx2.Request) -> httpx2.Response:
        doi = request.url.params["q"].removeprefix('doi:"').removesuffix('"')
        rows = [
            # A search result carries no links.bucket (only the full deposition
            # from GET .../depositions/{id} does), so strip it here.
            {**dep, "links": {k: v for k, v in dep["links"].items() if k != "bucket"}}
            for dep_id, dep in self.depositions.items()
            if dep_id not in self.published and dep["metadata"].get("doi") == doi
        ]
        return httpx2.Response(200, request=request, json=rows)

    def _create(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = self.next_id
        self.next_id += 1
        metadata = json.loads(request.content)["metadata"]
        deposition = {
            "id": dep_id,
            "state": "unsubmitted",
            "metadata": metadata,
            "links": {
                "bucket": f"https://zenodo.example/bucket/{dep_id}",
                "html": f"https://zenodo.example/deposit/{dep_id}",
            },
        }
        self.depositions[dep_id] = deposition
        self.files[dep_id] = []
        return httpx2.Response(201, request=request, json=deposition)

    def _upload(self, request: httpx2.Request) -> httpx2.Response:
        _, dep_id, filename = request.url.path.rsplit("/", 2)
        files = self.files[int(dep_id)]
        if filename not in files:  # a same-key PUT overwrites in place
            files.append(filename)
        return httpx2.Response(
            201, request=request, json={"key": filename, "size": len(request.content)}
        )

    def _publish(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.split("/")[-3])
        if not self.files[dep_id]:
            return httpx2.Response(400, request=request, json={"message": "no files"})
        self.published.add(dep_id)
        self.depositions[dep_id]["state"] = "done"
        # Zenodo mints its own DOI when the metadata does not carry one.
        meta = self.depositions[dep_id]["metadata"]
        self.depositions[dep_id]["doi"] = meta.get("doi") or f"10.5072/zenodo.{dep_id}"
        # Zenodo only creates a concept DOI for a DOI it minted itself, and
        # versioning is keyed on that concept.
        if not meta.get("doi"):
            self.depositions[dep_id]["conceptdoi"] = f"10.5072/zenodo.{dep_id - 1}"
        return httpx2.Response(
            202,
            request=request,
            json={
                "id": dep_id,
                "links": {"html": f"https://zenodo.example/records/{dep_id}"},
            },
        )

    def _delete(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.split("/")[-1])
        if dep_id in self.published:
            return httpx2.Response(403, request=request, json={})
        self.depositions.pop(dep_id, None)
        return httpx2.Response(204, request=request)


@pytest.fixture
def fake_zenodo() -> FakeZenodo:
    return FakeZenodo()


@pytest.fixture
def client(fake_zenodo: FakeZenodo) -> ZenodoClient:
    return ZenodoClient(
        "https://zenodo.example",
        "token",
        transport=httpx2.MockTransport(fake_zenodo.handler),
        sleep=lambda _: None,
    )


@pytest.fixture
def datacite_client() -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(datacite_handler))
