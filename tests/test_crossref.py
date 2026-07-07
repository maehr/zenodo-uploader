"""Tests for Crossref parsing and the DataCite→Crossref resolver fallback."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from zenodo_uploader.crossref import fetch_doi, parse_crossref
from zenodo_uploader.mapping import work_to_zenodo
from zenodo_uploader.resolve import fetch_work

CROSSREF_BOOK: dict[str, Any] = {
    "DOI": "10.30965/9783657796823",
    "type": "monograph",
    "title": ["Wie der Verwaltungscomputer die Arbeitsmigration programmierte"],
    "author": [{"given": "Moritz", "family": "Mähr", "sequence": "first"}],
    "publisher": "Brill | Schöningh",
    "issued": {"date-parts": [[2024, 10, 28]]},
    "resource": {"primary": {"URL": "https://brill.com/view/title/71011"}},
    "ISBN": ["9783657796823"],
}


def _registry_transport(datacite_status: int, crossref_status: int) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.datacite.org":
            payload = {
                "data": {
                    "attributes": {
                        "doi": "10.1/x",
                        "titles": [{"title": "From DataCite"}],
                        "publicationYear": 2020,
                    }
                }
            }
            return httpx2.Response(datacite_status, request=request, json=payload)
        return httpx2.Response(crossref_status, request=request, json={"message": CROSSREF_BOOK})

    return httpx2.MockTransport(handler)


def test_parse_crossref_book() -> None:
    record = parse_crossref(CROSSREF_BOOK)
    assert record.doi == "10.30965/9783657796823"
    assert record.resource_type_general == "Book"
    assert record.publication_year == 2024
    assert record.publication_date == "2024-10-28"
    assert record.creators[0].name == "Mähr, Moritz"
    assert record.landing_url == "https://brill.com/view/title/71011"
    assert record.license_id is None


def test_crossref_maps_to_zenodo_with_full_date() -> None:
    metadata = work_to_zenodo(parse_crossref(CROSSREF_BOOK))
    payload = metadata.to_payload()["metadata"]
    assert payload["publication_type"] == "book"
    assert payload["publication_date"] == "2024-10-28"
    assert payload["doi"] == "10.30965/9783657796823"
    assert payload["imprint_publisher"] == "Brill | Schöningh"


def test_parse_crossref_extras() -> None:
    record = parse_crossref(
        CROSSREF_BOOK
        | {
            "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
            "abstract": "<jats:p>Ein <jats:i>Abstract</jats:i>.</jats:p>",
            "editor": [{"given": "Ed", "family": "Itor", "ORCID": "http://orcid.org/0-2"}],
            "language": "de",
        }
    )
    assert record.license_id == "cc-by-4.0"
    assert record.description == "<p>Ein Abstract.</p>"
    assert record.creators[-1].is_editor and record.creators[-1].orcid == "0-2"
    assert record.language == "de"


def test_fetch_doi_crossref() -> None:
    with httpx2.Client(transport=_registry_transport(404, 200)) as client:
        assert fetch_doi(client, "10.30965/9783657796823")["type"] == "monograph"


def test_fetch_work_prefers_datacite() -> None:
    with httpx2.Client(transport=_registry_transport(200, 200)) as client:
        record = fetch_work(client, "10.1/x")
    assert record.title == "From DataCite"


def test_fetch_work_falls_back_to_crossref_on_404() -> None:
    with httpx2.Client(transport=_registry_transport(404, 200)) as client:
        record = fetch_work(client, "10.30965/9783657796823")
    assert record.title.startswith("Wie der Verwaltungscomputer")


def test_fetch_work_propagates_other_datacite_errors() -> None:
    with (
        httpx2.Client(transport=_registry_transport(500, 200)) as client,
        pytest.raises(httpx2.HTTPStatusError),
    ):
        fetch_work(client, "10.1/x")


def test_fetch_work_unknown_everywhere() -> None:
    with (
        httpx2.Client(transport=_registry_transport(404, 404)) as client,
        pytest.raises(httpx2.HTTPStatusError),
    ):
        fetch_work(client, "10.1/nowhere")


def test_parse_crossref_blank_abstract_and_year_fallback() -> None:
    record = parse_crossref(
        {
            "DOI": "10.1/x",
            "type": "dataset",
            "title": ["T"],
            "abstract": "<jats:p>   </jats:p>",
        }
    )
    assert record.description is None
    assert record.publication_year == 0
    assert record.publication_date is None
    assert record.resource_type_general == "Dataset"
