"""Fetch and parse DOI metadata from the Crossref REST API."""

from __future__ import annotations

import re
from typing import Any

import httpx2

from .models import Creator, WorkRecord

CROSSREF_API = "https://api.crossref.org/works/"

_TYPE_MAP = {
    "monograph": "Book",
    "book": "Book",
    "edited-book": "Book",
    "reference-book": "Book",
    "book-chapter": "BookChapter",
    "book-section": "BookChapter",
    "book-part": "BookChapter",
    "journal-article": "JournalArticle",
    "proceedings-article": "ConferencePaper",
    "report": "Report",
    "posted-content": "Preprint",
    "dissertation": "Dissertation",
    "dataset": "Dataset",
}

_CC_LICENSE_RE = re.compile(
    r"creativecommons\.org/(?:licenses/(?P<code>[a-z-]+)/(?P<version>\d\.\d)"
    r"|(?P<zero>publicdomain/zero/1\.0))"
)
_JATS_TAG_RE = re.compile(r"<[^>]+>")


def fetch_doi(client: httpx2.Client, doi: str) -> dict[str, Any]:
    """Fetch the raw Crossref ``message`` dict for ``doi``."""
    response = client.get(CROSSREF_API + doi)
    response.raise_for_status()
    message: dict[str, Any] = response.json()["message"]
    return message


def license_id_from_url(url: str) -> str | None:
    """Derive an SPDX-ish Zenodo license id from a Creative Commons URL.

    Examples:
        >>> license_id_from_url("https://creativecommons.org/licenses/by-nc/4.0/")
        'cc-by-nc-4.0'
        >>> license_id_from_url("https://creativecommons.org/publicdomain/zero/1.0/")
        'cc0-1.0'
        >>> license_id_from_url("https://example.org/proprietary") is None
        True
    """
    match = _CC_LICENSE_RE.search(url)
    if not match:
        return None
    if match.group("zero"):
        return "cc0-1.0"
    return f"cc-{match.group('code')}-{match.group('version')}"


def parse_contributor(raw: dict[str, Any], *, is_editor: bool = False) -> Creator:
    """Parse a Crossref author/editor entry.

    Examples:
        >>> parse_contributor({"given": "Jane", "family": "Doe",
        ...     "ORCID": "http://orcid.org/0000-0002-1825-0097"})
        ... # doctest: +NORMALIZE_WHITESPACE
        Creator(name='Doe, Jane', orcid='0000-0002-1825-0097', affiliation=None,
                is_editor=False)
        >>> parse_contributor({"name": "Some Consortium"}).name
        'Some Consortium'
    """
    if raw.get("family") and raw.get("given"):
        name = f"{raw['family']}, {raw['given']}"
    else:
        name = str(raw.get("name") or raw.get("family") or raw.get("given"))
    orcid = None
    if raw.get("ORCID"):
        orcid = str(raw["ORCID"]).rsplit("/", 1)[-1]
    return Creator(name=name, orcid=orcid, is_editor=is_editor)


def publication_date_from(message: dict[str, Any]) -> tuple[int, str] | None:
    """Extract (year, ISO date) from Crossref date-parts, padding as needed.

    Examples:
        >>> publication_date_from({"issued": {"date-parts": [[2024, 10, 28]]}})
        (2024, '2024-10-28')
        >>> publication_date_from({"issued": {"date-parts": [[2022]]}})
        (2022, '2022-01-01')
        >>> publication_date_from({}) is None
        True
    """
    for field in ("issued", "published", "created"):
        parts = (message.get(field) or {}).get("date-parts") or [[]]
        first = parts[0]
        if first and first[0]:
            year, month, day = [*first, 1, 1][:3]
            return int(year), f"{year:04d}-{month:02d}-{day:02d}"
    return None


def parse_crossref(message: dict[str, Any]) -> WorkRecord:
    """Convert a Crossref ``message`` dict into a :class:`WorkRecord`.

    Examples:
        >>> record = parse_crossref({
        ...     "DOI": "10.30965/9783657796823",
        ...     "type": "monograph",
        ...     "title": ["Wie der Verwaltungscomputer die Arbeitsmigration programmierte"],
        ...     "author": [{"given": "Moritz", "family": "M\\xe4hr"}],
        ...     "publisher": "Brill | Sch\\xf6ningh",
        ...     "issued": {"date-parts": [[2024, 10, 28]]},
        ...     "resource": {"primary": {"URL": "https://brill.com/view/title/71011"}},
        ... })
        >>> record.resource_type_general, record.publication_date
        ('Book', '2024-10-28')
    """
    issued = publication_date_from(message)
    license_id = None
    for entry in message.get("license") or []:
        license_id = license_id or license_id_from_url(str(entry.get("URL", "")))
    description = None
    if message.get("abstract"):
        text = _JATS_TAG_RE.sub("", str(message["abstract"])).strip()
        if text:
            description = f"<p>{text}</p>"
    creators = [parse_contributor(a) for a in message.get("author") or []]
    creators += [parse_contributor(e, is_editor=True) for e in message.get("editor") or []]
    return WorkRecord(
        doi=str(message["DOI"]).lower(),
        title=message["title"][0],
        publication_year=issued[0] if issued else 0,
        publication_date=issued[1] if issued else None,
        creators=creators,
        publisher=message.get("publisher"),
        resource_type_general=_TYPE_MAP.get(str(message.get("type"))),
        license_id=license_id,
        landing_url=((message.get("resource") or {}).get("primary") or {}).get("URL"),
        language=message.get("language"),
        description=description,
    )
