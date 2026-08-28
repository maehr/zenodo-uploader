"""zenodo-uploader: mirror DOIs and upload records to Zenodo."""

from .crossref import parse_crossref
from .datacite import parse_datacite
from .mapping import work_to_zenodo
from .models import Creator, RelatedIdentifier, WorkRecord, ZenodoMetadata
from .resolve import fetch_work
from .zenodo import ZenodoClient, ZenodoError

__all__ = [
    "Creator",
    "RelatedIdentifier",
    "WorkRecord",
    "ZenodoClient",
    "ZenodoError",
    "ZenodoMetadata",
    "fetch_work",
    "parse_crossref",
    "parse_datacite",
    "work_to_zenodo",
]
