"""Tests for DataCite parsing and the DataCite → Zenodo mapping."""

from __future__ import annotations

import httpx2
import pytest

from tests.conftest import DATACITE_CHAPTER
from zenodo_uploader.datacite import fetch_doi, parse_datacite, parse_related
from zenodo_uploader.mapping import default_description, upload_type_for, work_to_zenodo
from zenodo_uploader.models import Creator, RelatedIdentifier, WorkRecord, ZenodoMetadata


def test_parse_datacite_full_record() -> None:
    record = parse_datacite(DATACITE_CHAPTER)
    assert record.doi == "10.21255/sgb-03.05-238056"
    assert record.title == "Hand-Werk und Lohn-Arbeit"
    assert record.creators == [
        Creator(name="Hitz, Benjamin", orcid="0000-0002-3208-4881", is_editor=False)
    ]
    assert record.license_id == "cc-by-nc-4.0"
    assert record.related_identifiers == [
        RelatedIdentifier(relation="isPartOf", identifier="10.21255/sgb-03-345800")
    ]


def test_parse_datacite_minimal_record() -> None:
    record = parse_datacite({"doi": "10.1/x", "titles": [{"title": "T"}], "publicationYear": 2020})
    assert record.creators == []
    assert record.license_id is None
    assert record.description is None
    assert record.related_identifiers == []


def test_parse_related_skips_unknown_relations() -> None:
    related = parse_related(
        [
            {"relationType": "Nonsense", "relatedIdentifier": "x"},
            {
                "relationType": "HasPart",
                "relatedIdentifier": "10.1/part",
                "relatedIdentifierType": "DOI",
            },
        ]
    )
    assert [r.relation for r in related] == ["hasPart"]


def test_fetch_doi_returns_attributes() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/dois/10.1/x"
        return httpx2.Response(
            200, request=request, json={"data": {"attributes": {"doi": "10.1/x"}}}
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        assert fetch_doi(client, "10.1/x") == {"doi": "10.1/x"}


def test_fetch_doi_raises_on_missing() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request, json={})

    with (
        httpx2.Client(transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(httpx2.HTTPStatusError),
    ):
        fetch_doi(client, "10.1/missing")


def test_mapping_chapter_to_zenodo() -> None:
    record = parse_datacite(DATACITE_CHAPTER)
    metadata = work_to_zenodo(record, community="stadt-geschichte-basel")
    payload = metadata.to_payload()["metadata"]
    assert payload["upload_type"] == "publication"
    assert payload["publication_type"] == "section"
    assert payload["doi"] == "10.21255/sgb-03.05-238056"
    assert payload["license"] == "cc-by-nc-4.0"
    assert payload["language"] == "deu"
    assert payload["imprint_publisher"] == "Christoph Merian Verlag"
    assert payload["communities"] == [{"identifier": "stadt-geschichte-basel"}]
    assert payload["publication_date"] == "2024-01-01"
    assert {"relation": "isPartOf", "identifier": "10.21255/sgb-03-345800"} in payload[
        "related_identifiers"
    ]
    assert {
        "relation": "isIdenticalTo",
        "identifier": DATACITE_CHAPTER["url"],
    } in payload["related_identifiers"]
    assert payload["description"].startswith("<p>Hand-Werk und Lohn-Arbeit.")


def test_mapping_overrides_and_extras() -> None:
    record = parse_datacite(DATACITE_CHAPTER)
    metadata = work_to_zenodo(
        record,
        description="<p>Custom</p>",
        extra_related=[RelatedIdentifier(relation="hasPart", identifier="10.1/y")],
        keep_doi=False,
    )
    payload = metadata.to_payload()["metadata"]
    assert payload["description"] == "<p>Custom</p>"
    assert "doi" not in payload
    assert "communities" not in payload
    assert {"relation": "hasPart", "identifier": "10.1/y"} in payload["related_identifiers"]


def test_mapping_keeps_existing_description_and_language() -> None:
    record = WorkRecord(
        doi="10.1/x",
        title="T",
        publication_year=2021,
        description="original",
        language="rm",
        resource_type_general="Dataset",
    )
    metadata = work_to_zenodo(record)
    assert metadata.description == "original"
    assert metadata.language == "rm"
    assert metadata.imprint_publisher is None
    assert metadata.publication_type is None


def test_upload_type_fallback() -> None:
    assert upload_type_for("Sound") == ("publication", "other")


def test_default_description_without_optional_fields() -> None:
    record = WorkRecord(doi="10.1/x", title="T", publication_year=2024)
    text = default_description(record)
    assert "10.1/x" in text
    assert "und" not in text


def test_payload_creator_details_and_keywords() -> None:
    metadata = ZenodoMetadata(
        title="T",
        upload_type="publication",
        description="D",
        creators=[Creator(name="Doe, Jane", orcid="0-1", affiliation="Basel")],
        publication_date="2024-01-01",
        keywords=["a"],
        related_identifiers=[
            RelatedIdentifier(relation="cites", identifier="10.1/z", resource_type="publication")
        ],
    )
    payload = metadata.to_payload()["metadata"]
    assert payload["creators"] == [{"name": "Doe, Jane", "orcid": "0-1", "affiliation": "Basel"}]
    assert payload["keywords"] == ["a"]
    assert payload["related_identifiers"] == [
        {"relation": "cites", "identifier": "10.1/z", "resource_type": "publication"}
    ]


def test_parse_datacite_edge_branches() -> None:
    record = parse_datacite(
        {
            "doi": "10.1/x",
            "titles": [{"title": "T"}],
            "publicationYear": 2020,
            "creators": [
                {
                    "name": "Doe, Jane",
                    "nameIdentifiers": [
                        {"nameIdentifierScheme": "ISNI", "nameIdentifier": "isni-1"}
                    ],
                }
            ],
            "rightsList": [{"rights": "unnamed license"}],
            "descriptions": [{"description": ""}, {"description": "second"}],
            "relatedIdentifiers": [
                {
                    "relationType": "References",
                    "relatedIdentifier": "https://example.org/r",
                    "relatedIdentifierType": "URL",
                }
            ],
        }
    )
    assert record.creators[0].orcid is None
    assert record.license_id is None
    assert record.description == "second"
    assert record.related_identifiers[0].identifier == "https://example.org/r"


def test_payload_imprint_place() -> None:
    metadata = ZenodoMetadata(
        title="T",
        upload_type="publication",
        description="D",
        creators=[Creator(name="Doe, Jane")],
        publication_date="2024-01-01",
        imprint_place="Basel",
    )
    assert metadata.to_payload()["metadata"]["imprint_place"] == "Basel"


def test_mapping_deduplicates_related() -> None:
    record = parse_datacite(DATACITE_CHAPTER)
    metadata = work_to_zenodo(
        record,
        extra_related=[RelatedIdentifier(relation="isPartOf", identifier="10.21255/sgb-03-345800")],
    )
    keys = [(r.relation, r.identifier) for r in metadata.related_identifiers]
    assert len(keys) == len(set(keys))


def test_parse_datacite_full_issued_date() -> None:
    record = parse_datacite(
        {
            "doi": "10.1/x",
            "titles": [{"title": "T"}],
            "publicationYear": 2024,
            "dates": [{"dateType": "Issued", "date": "2024-10-28"}],
        }
    )
    assert record.publication_date == "2024-10-28"
    assert work_to_zenodo(record).publication_date == "2024-10-28"


def test_payload_book_imprint_and_alternate_ids() -> None:
    metadata = ZenodoMetadata(
        title="B",
        upload_type="publication",
        publication_type="book",
        description="D",
        creators=[Creator(name="Doe, Jane")],
        publication_date="2024-01-01",
        imprint_isbn="978-3-03969-003-9",
        imprint_place="Basel",
        partof_title="The Book",
        partof_pages="1-20",
        related_identifiers=[
            RelatedIdentifier(
                relation="isAlternateIdentifier", identifier="978-3-03969-003-9", scheme="isbn"
            ),
            RelatedIdentifier(
                relation="hasPart", identifier="10.1/c", resource_type="publication-section"
            ),
        ],
    )
    m = metadata.to_payload()["metadata"]
    assert m["imprint_isbn"] == "978-3-03969-003-9"
    assert m["imprint_place"] == "Basel"
    assert m["partof_title"] == "The Book" and m["partof_pages"] == "1-20"
    rel = {r["identifier"]: r for r in m["related_identifiers"]}
    assert rel["978-3-03969-003-9"]["scheme"] == "isbn"
    assert rel["10.1/c"]["resource_type"] == "publication-section"
