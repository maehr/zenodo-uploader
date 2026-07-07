"""Map registry-neutral work records to Zenodo deposit metadata."""

from __future__ import annotations

from .models import RelatedIdentifier, WorkRecord, ZenodoMetadata

_UPLOAD_TYPES: dict[str, tuple[str, str | None]] = {
    "Book": ("publication", "book"),
    "BookChapter": ("publication", "section"),
    "JournalArticle": ("publication", "article"),
    "ConferencePaper": ("publication", "conferencepaper"),
    "Report": ("publication", "report"),
    "Preprint": ("publication", "preprint"),
    "Dissertation": ("publication", "thesis"),
    "Dataset": ("dataset", None),
    "Software": ("software", None),
    "Image": ("image", None),
    "Audiovisual": ("video", None),
    "Text": ("publication", "other"),
}

_LANGUAGE_MAP = {"de": "deu", "en": "eng", "fr": "fra", "it": "ita"}


def upload_type_for(resource_type_general: str | None) -> tuple[str, str | None]:
    """Map a DataCite ``resourceTypeGeneral`` to Zenodo upload/publication type.

    Examples:
        >>> upload_type_for("BookChapter")
        ('publication', 'section')
        >>> upload_type_for("Dataset")
        ('dataset', None)
        >>> upload_type_for(None)
        ('publication', 'other')
    """
    return _UPLOAD_TYPES.get(resource_type_general or "", ("publication", "other"))


def default_description(record: WorkRecord) -> str:
    """Build a minimal HTML description for records without one.

    Zenodo requires a non-empty description; DataCite records often lack one.

    Examples:
        >>> record = WorkRecord(doi="10.1/x", title="T", publication_year=2024,
        ...     publisher="P", landing_url="https://example.org/x")
        >>> default_description(record)
        '<p>T. P, 2024.</p><p>Verf\\xfcgbar unter <a href="https://doi.org/10.1/x">\
doi.org/10.1/x</a> und <a href="https://example.org/x">example.org/x</a>.</p>'
    """
    first = f"<p>{record.title}."
    if record.publisher:
        first += f" {record.publisher},"
    first += f" {record.publication_year}.</p>"
    links = f'<a href="https://doi.org/{record.doi}">doi.org/{record.doi}</a>'
    if record.landing_url:
        label = record.landing_url.removeprefix("https://").removeprefix("http://")
        links += f' und <a href="{record.landing_url}">{label}</a>'
    return first + f"<p>Verfügbar unter {links}.</p>"


def work_to_zenodo(
    record: WorkRecord,
    *,
    community: str | None = None,
    description: str | None = None,
    extra_related: list[RelatedIdentifier] | None = None,
    keep_doi: bool = True,
) -> ZenodoMetadata:
    """Map a :class:`WorkRecord` onto Zenodo deposit metadata.

    With ``keep_doi`` (the default) the existing DOI is written into the
    deposit so Zenodo does not mint a new one.

    Examples:
        >>> record = WorkRecord(doi="10.1/x", title="T", publication_year=2024,
        ...     resource_type_general="Book", publisher="P", language="de",
        ...     license_id="cc-by-nc-4.0")
        >>> metadata = work_to_zenodo(record, community="c1")
        >>> metadata.upload_type, metadata.publication_type, metadata.doi
        ('publication', 'book', '10.1/x')
        >>> metadata.communities, metadata.language, metadata.license
        (['c1'], 'deu', 'cc-by-nc-4.0')
        >>> work_to_zenodo(record, keep_doi=False).doi is None
        True
    """
    upload_type, publication_type = upload_type_for(record.resource_type_general)
    related: list[RelatedIdentifier] = []
    seen: set[tuple[str, str]] = set()
    candidates = list(record.related_identifiers) + list(extra_related or [])
    if record.landing_url:
        candidates.append(
            RelatedIdentifier(relation="isIdenticalTo", identifier=record.landing_url)
        )
    for item in candidates:
        key = (item.relation, item.identifier)
        if key not in seen:
            seen.add(key)
            related.append(item)
    language = record.language
    if language in _LANGUAGE_MAP:
        language = _LANGUAGE_MAP[language]
    return ZenodoMetadata(
        title=record.title,
        upload_type=upload_type,
        publication_type=publication_type,
        description=description or record.description or default_description(record),
        creators=record.creators,
        publication_date=record.publication_date or f"{record.publication_year}-01-01",
        doi=record.doi if keep_doi else None,
        license=record.license_id,
        language=language,
        imprint_publisher=record.publisher if upload_type == "publication" else None,
        keywords=[],
        communities=[community] if community else [],
        related_identifiers=related,
    )
