"""Shared fixtures: a fake in-memory Zenodo served via httpx2.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from zenodo_uploader.zenodo import ZenodoClient

DATACITE_CHAPTER: dict[str, Any] = {
    "doi": "10.21255/sgb-03.05-238056",
    "titles": [{"title": "Hand-Werk und Lohn-Arbeit"}],
    "creators": [
        {
            "name": "Hitz, Benjamin",
            "givenName": "Benjamin",
            "familyName": "Hitz",
            "nameIdentifiers": [
                {
                    "nameIdentifierScheme": "ORCID",
                    "nameIdentifier": "https://orcid.org/0000-0002-3208-4881",
                }
            ],
        }
    ],
    "publisher": "Christoph Merian Verlag",
    "publicationYear": 2024,
    "types": {"resourceTypeGeneral": "BookChapter"},
    "rightsList": [{"rightsIdentifier": "cc-by-nc-4.0"}],
    "url": "https://emono.unibas.ch/stadtgeschichtebasel/catalog/book/band3/chapter/306",
    "language": "de",
    "descriptions": [],
    "relatedIdentifiers": [
        {
            "relationType": "IsPartOf",
            "relatedIdentifier": "https://doi.org/10.21255/sgb-03-345800",
            "relatedIdentifierType": "DOI",
        }
    ],
}


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
            if request.method == "DELETE":
                return self._delete_file(request)
            return self._upload(request)
        if path.endswith("/actions/publish"):
            return self._publish(request)
        if path.endswith("/draft/review"):
            if request.method == "DELETE":
                return self._cancel_review(request)
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

    def _cancel_review(self, request: httpx2.Request) -> httpx2.Response:
        record_id = int(request.url.path.split("/")[3])
        self.reviews.pop(record_id, None)
        self.submitted.discard(record_id)
        return httpx2.Response(204, request=request)

    def _delete_file(self, request: httpx2.Request) -> httpx2.Response:
        _, dep_id, filename = request.url.path.rsplit("/", 2)
        files = self.files.get(int(dep_id), [])
        if filename not in files:
            return httpx2.Response(404, request=request, json={})
        files.remove(filename)
        return httpx2.Response(204, request=request)

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
            dep
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
        self.files[int(dep_id)].append(filename)
        return httpx2.Response(
            201, request=request, json={"key": filename, "size": len(request.content)}
        )

    def _publish(self, request: httpx2.Request) -> httpx2.Response:
        dep_id = int(request.url.path.split("/")[-3])
        if not self.files[dep_id]:
            return httpx2.Response(400, request=request, json={"message": "no files"})
        self.published.add(dep_id)
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
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, request=request, json={"data": {"attributes": DATACITE_CHAPTER}}
        )

    return httpx2.Client(transport=httpx2.MockTransport(handler))
