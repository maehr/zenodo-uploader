"""Fetch and parse DOI metadata from the DataCite REST API."""

from __future__ import annotations

import re
from typing import Any

import httpx2

from .models import Creator, RelatedIdentifier, RelationType, WorkRecord

DATACITE_API = "https://api.datacite.org/dois/"

_RELATION_MAP: dict[str, RelationType] = {
    "IsPartOf": "isPartOf",
    "HasPart": "hasPart",
    "IsIdenticalTo": "isIdenticalTo",
    "IsNewVersionOf": "isNewVersionOf",
    "IsPreviousVersionOf": "isPreviousVersionOf",
    "References": "references",
    "IsReferencedBy": "isReferencedBy",
    "IsSupplementTo": "isSupplementTo",
    "IsSupplementedBy": "isSupplementedBy",
    "IsDerivedFrom": "isDerivedFrom",
    "IsSourceOf": "isSourceOf",
    "Cites": "cites",
    "IsCitedBy": "isCitedBy",
}


def fetch_doi(client: httpx2.Client, doi: str) -> dict[str, Any]:
    """Fetch the raw DataCite attribute dict for ``doi``."""
    response = client.get(DATACITE_API + doi)
    response.raise_for_status()
    attributes: dict[str, Any] = response.json()["data"]["attributes"]
    return attributes


def parse_creator(raw: dict[str, Any]) -> Creator:
    """Parse a DataCite creator entry.

    Examples:
        >>> parse_creator({"name": "Doe, Jane", "nameIdentifiers": [
        ...     {"nameIdentifierScheme": "ORCID",
        ...      "nameIdentifier": "https://orcid.org/0000-0002-1825-0097"}]})
        ... # doctest: +NORMALIZE_WHITESPACE
        Creator(name='Doe, Jane', orcid='0000-0002-1825-0097', affiliation=None,
                is_editor=False)
        >>> parse_creator({"name": "Baur, Esther (ed.)"}).is_editor
        True
    """
    name = str(raw["name"])
    is_editor = name.endswith(" (ed.)")
    name = name.removesuffix(" (ed.)")
    orcid = None
    for identifier in raw.get("nameIdentifiers", []):
        if identifier.get("nameIdentifierScheme") == "ORCID":
            orcid = str(identifier["nameIdentifier"]).rsplit("/", 1)[-1]
    return Creator(name=name, orcid=orcid, is_editor=is_editor)


def parse_related(raw: list[dict[str, Any]]) -> list[RelatedIdentifier]:
    """Parse DataCite ``relatedIdentifiers`` into normalized relations.

    DOI identifiers expressed as ``https://doi.org/...`` URLs are reduced to
    bare DOIs; unknown relation types are skipped.

    Examples:
        >>> parse_related([{"relationType": "IsPartOf",
        ...     "relatedIdentifier": "https://doi.org/10.1/x",
        ...     "relatedIdentifierType": "DOI"}])[0].identifier
        '10.1/x'
        >>> parse_related([{"relationType": "Compiles", "relatedIdentifier": "x"}])
        []
    """
    related: list[RelatedIdentifier] = []
    for entry in raw:
        relation = _RELATION_MAP.get(str(entry.get("relationType")))
        if relation is None:
            continue
        identifier = str(entry["relatedIdentifier"])
        if entry.get("relatedIdentifierType") == "DOI":
            identifier = identifier.removeprefix("https://doi.org/")
        related.append(RelatedIdentifier(relation=relation, identifier=identifier))
    return related


def issued_date(attributes: dict[str, Any]) -> str | None:
    """Return a full ISO ``Issued`` date from DataCite ``dates``, if present.

    Year-only values (the common case) return ``None`` so the caller can fall
    back to ``publicationYear``.

    Examples:
        >>> issued_date({"dates": [{"dateType": "Issued", "date": "2024-10-28"}]})
        '2024-10-28'
        >>> issued_date({"dates": [{"dateType": "Issued", "date": "2024"}]}) is None
        True
        >>> issued_date({}) is None
        True
    """
    for entry in attributes.get("dates", []):
        if entry.get("dateType") == "Issued" and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", str(entry.get("date", ""))
        ):
            return str(entry["date"])
    return None


def parse_datacite(attributes: dict[str, Any]) -> WorkRecord:
    """Convert a DataCite attribute dict into a :class:`WorkRecord`.

    Examples:
        >>> record = parse_datacite({
        ...     "doi": "10.1000/x",
        ...     "titles": [{"title": "A Title"}],
        ...     "creators": [{"name": "Doe, Jane"}],
        ...     "publisher": "P",
        ...     "publicationYear": 2024,
        ...     "types": {"resourceTypeGeneral": "BookChapter"},
        ...     "rightsList": [{"rightsIdentifier": "cc-by-nc-4.0"}],
        ...     "url": "https://example.org/landing",
        ...     "language": "de",
        ...     "descriptions": [{"description": "About the work"}],
        ...     "relatedIdentifiers": [],
        ... })
        >>> record.title, record.license_id, record.description
        ('A Title', 'cc-by-nc-4.0', 'About the work')
    """
    license_id = None
    for rights in attributes.get("rightsList", []):
        if rights.get("rightsIdentifier"):
            license_id = str(rights["rightsIdentifier"])
    description = None
    for entry in attributes.get("descriptions", []):
        if entry.get("description"):
            description = str(entry["description"])
            break
    return WorkRecord(
        doi=attributes["doi"],
        title=attributes["titles"][0]["title"],
        publication_year=attributes["publicationYear"],
        publication_date=issued_date(attributes),
        creators=[parse_creator(c) for c in attributes.get("creators", [])],
        publisher=attributes.get("publisher"),
        resource_type_general=(attributes.get("types") or {}).get("resourceTypeGeneral"),
        license_id=license_id,
        landing_url=attributes.get("url"),
        language=attributes.get("language"),
        description=description,
        related_identifiers=parse_related(attributes.get("relatedIdentifiers", [])),
    )
