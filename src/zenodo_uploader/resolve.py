"""Resolve a DOI to a :class:`WorkRecord`, trying DataCite then Crossref."""

from __future__ import annotations

import httpx2
import structlog

from . import crossref, datacite
from .models import WorkRecord

log = structlog.get_logger()


def fetch_work(client: httpx2.Client, doi: str) -> WorkRecord:
    """Fetch DOI metadata from DataCite, falling back to Crossref on 404."""
    try:
        record = datacite.parse_datacite(datacite.fetch_doi(client, doi))
        log.info("resolved", doi=doi, registry="datacite")
        return record
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    record = crossref.parse_crossref(crossref.fetch_doi(client, doi))
    log.info("resolved", doi=doi, registry="crossref")
    return record
