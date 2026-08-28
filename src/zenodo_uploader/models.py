"""Data models for DataCite records and Zenodo deposition metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

RelationType = Literal[
    "isCitedBy",
    "cites",
    "isSupplementTo",
    "isSupplementedBy",
    "isContinuedBy",
    "continues",
    "isDescribedBy",
    "describes",
    "hasMetadata",
    "isMetadataFor",
    "isNewVersionOf",
    "isPreviousVersionOf",
    "isPartOf",
    "hasPart",
    "isReferencedBy",
    "references",
    "isDocumentedBy",
    "documents",
    "isCompiledBy",
    "compiles",
    "isVariantFormOf",
    "isOriginalFormof",
    "isIdenticalTo",
    "isAlternateIdentifier",
    "isReviewedBy",
    "reviews",
    "isDerivedFrom",
    "isSourceOf",
    "requires",
    "isRequiredBy",
    "isObsoletedBy",
    "obsoletes",
]


class Creator(BaseModel):
    """A creator of a work.

    Examples:
        >>> Creator(name="Doe, Jane", orcid="0000-0002-1825-0097").name
        'Doe, Jane'
    """

    name: str
    orcid: str | None = None
    affiliation: str | None = None
    is_editor: bool = False


class RelatedIdentifier(BaseModel):
    """A related identifier with its relation to the record.

    Examples:
        >>> RelatedIdentifier(relation="isPartOf", identifier="10.5555/example-book").relation
        'isPartOf'
    """

    relation: RelationType
    identifier: str
    resource_type: str | None = None
    scheme: str | None = None


class WorkRecord(BaseModel):
    """Registry-neutral DOI metadata (from DataCite or Crossref) for mirroring.

    Examples:
        >>> WorkRecord(doi="10.1000/x", title="T", publication_year=2024).doi
        '10.1000/x'
    """

    doi: str
    title: str
    publication_year: int
    publication_date: str | None = None  # full ISO date when the registry has one
    creators: list[Creator] = Field(default_factory=list)
    publisher: str | None = None
    resource_type_general: str | None = None
    license_id: str | None = None
    landing_url: str | None = None
    language: str | None = None
    description: str | None = None
    related_identifiers: list[RelatedIdentifier] = Field(default_factory=list)


class ZenodoMetadata(BaseModel):
    """Metadata for the Zenodo legacy deposit API.

    Examples:
        >>> m = ZenodoMetadata(title="T", upload_type="publication",
        ...     description="D", creators=[Creator(name="Doe, Jane")],
        ...     publication_date="2024-01-01")
        >>> m.to_payload()["metadata"]["title"]
        'T'
    """

    title: str
    upload_type: str
    publication_type: str | None = None
    description: str
    creators: list[Creator]
    publication_date: str
    doi: str | None = None
    license: str | None = None
    language: str | None = None
    imprint_publisher: str | None = None
    imprint_place: str | None = None
    imprint_isbn: str | None = None
    partof_title: str | None = None
    partof_pages: str | None = None
    keywords: list[str] = Field(default_factory=list)
    communities: list[str] = Field(default_factory=list)
    related_identifiers: list[RelatedIdentifier] = Field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the ``{"metadata": {...}}`` shape the deposit API expects.

        Optional fields that are unset are omitted entirely.

        Examples:
            >>> m = ZenodoMetadata(title="T", upload_type="publication",
            ...     description="D", creators=[Creator(name="Doe, Jane")],
            ...     publication_date="2024-01-01", communities=["c1"])
            >>> payload = m.to_payload()["metadata"]
            >>> payload["communities"]
            [{'identifier': 'c1'}]
            >>> "license" in payload
            False
        """
        metadata: dict[str, Any] = {
            "title": self.title,
            "upload_type": self.upload_type,
            "description": self.description,
            "creators": [
                {"name": c.name}
                | ({"orcid": c.orcid} if c.orcid else {})
                | ({"affiliation": c.affiliation} if c.affiliation else {})
                for c in self.creators
            ],
            "publication_date": self.publication_date,
        }
        if self.publication_type:
            metadata["publication_type"] = self.publication_type
        if self.doi:
            metadata["doi"] = self.doi
        if self.license:
            metadata["license"] = self.license
        if self.language:
            metadata["language"] = self.language
        if self.imprint_publisher:
            metadata["imprint_publisher"] = self.imprint_publisher
        if self.imprint_place:
            metadata["imprint_place"] = self.imprint_place
        if self.imprint_isbn:
            metadata["imprint_isbn"] = self.imprint_isbn
        if self.partof_title:
            metadata["partof_title"] = self.partof_title
        if self.partof_pages:
            metadata["partof_pages"] = self.partof_pages
        if self.keywords:
            metadata["keywords"] = list(self.keywords)
        if self.communities:
            metadata["communities"] = [{"identifier": c} for c in self.communities]
        if self.related_identifiers:
            metadata["related_identifiers"] = [
                {"relation": r.relation, "identifier": r.identifier}
                | ({"resource_type": r.resource_type} if r.resource_type else {})
                | ({"scheme": r.scheme} if r.scheme else {})
                for r in self.related_identifiers
            ]
        return {"metadata": metadata}


REQUIRED_DEPOSIT_FIELDS = ("title", "upload_type", "description", "publication_date", "creators")


def validate_deposit_metadata(meta: Mapping[str, Any]) -> None:
    """Check the minimum Zenodo deposit fields, raising ``ValueError`` if any are missing.

    Used by the ``upload`` command to give a clear error for hand-written or
    ``.zenodo.json`` files before they reach the API. Everything else in ``meta``
    is passed through to Zenodo verbatim.

    Examples:
        >>> validate_deposit_metadata({"title": "T", "upload_type": "software",
        ...     "description": "D", "publication_date": "2024-01-01",
        ...     "creators": [{"name": "Doe, Jane"}]})
        >>> validate_deposit_metadata({"title": "T", "upload_type": "software"})
        Traceback (most recent call last):
        ValueError: missing required field(s): description, publication_date, creators
        >>> validate_deposit_metadata({"title": "T", "upload_type": "software",
        ...     "description": "D", "publication_date": "2024-01-01",
        ...     "creators": [{"orcid": "0000-0002-1825-0097"}]})
        Traceback (most recent call last):
        ValueError: every entry in 'creators' needs a 'name'
    """
    missing = [field for field in REQUIRED_DEPOSIT_FIELDS if not meta.get(field)]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    creators = meta["creators"]
    if not isinstance(creators, list) or not all(
        isinstance(c, Mapping) and c.get("name") for c in creators
    ):
        raise ValueError("every entry in 'creators' needs a 'name'")
