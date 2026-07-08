"""Enrich the existing Stadt.Geschichte.Basel drafts with richer metadata.

Run AFTER mirror_sgb.py + `zenodo-uploader batch` have created the drafts. For
each DOI in the batch state file this:

  - adds publishing info (publisher, place, ISBN),
  - sets book part-of info (volume title + page range) on chapters,
  - adds ISBN and the emono landing page as alternate identifiers,
  - types every related identifier (volume = book, chapter = section),
  - sets the description to the chapter full text (from the local minimal HTML)
    or, for a volume, the emono book abstract,
  - attaches (but does not submit) the community-submission review.

Nothing is published; drafts stay private. Run from the repo root so the
``.env`` token is found:

    uv run --with beautifulsoup4 python examples/stadt-geschichte-basel/enrich_drafts.py \
        --state examples/stadt-geschichte-basel/prod-state.json \
        --source /path/to/sgb-minimal-html [--only DOI] [--no-review]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx2

from zenodo_uploader.config import Settings, base_url_for
from zenodo_uploader.models import Creator, RelatedIdentifier, ZenodoMetadata
from zenodo_uploader.zenodo import ZenodoClient

DATACITE_API = "https://api.datacite.org/dois/"
COMMUNITY = "stadt-geschichte-basel"
PUBLISHER, PLACE = "Christoph Merian Verlag", "Basel"
# Volumes 4 and 5 have no ISBN in DataCite; taken from emono (verified).
ISBN_FALLBACK = {4: "978-3-03969-004-6", 5: "978-3-03969-005-3"}
DOI_RE = re.compile(r"^10\.21255/(sgb-(\d{2})(?:\.(\d{2}))?-\d+)$")
MAIN_RE = re.compile(r"<main\b[^>]*>(.*?)</main>", re.S)
META_RE = '<meta name="{name}"[^>]*content="([^"]*)"'


def parse_doi(doi: str) -> tuple[str, int, int | None]:
    m = DOI_RE.match(doi)
    if not m:
        raise ValueError(doi)
    return m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None


def datacite(client: httpx2.Client, doi: str) -> dict:
    r = client.get(DATACITE_API + doi)
    r.raise_for_status()
    return r.json()["data"]["attributes"]


def isbn_of(attrs: dict) -> str | None:
    for ident in attrs.get("identifiers", []) + attrs.get("alternateIdentifiers", []):
        if (ident.get("identifierType") or ident.get("alternateIdentifierType")) == "ISBN":
            return ident.get("identifier") or ident.get("alternateIdentifier")
    return None


def creators_of(attrs: dict) -> list[Creator]:
    out = []
    for c in attrs.get("creators", []):
        name = c["name"].removesuffix(" (ed.)").strip()
        orcid = None
        for i in c.get("nameIdentifiers", []):
            if i.get("nameIdentifierScheme") == "ORCID":
                orcid = i["nameIdentifier"].strip().rsplit("/", 1)[-1]
        out.append(Creator(name=name, orcid=orcid))
    return out


def license_of(attrs: dict) -> str | None:
    for r in attrs.get("rightsList", []):
        if r.get("rightsIdentifier"):
            return r["rightsIdentifier"]
    return None


def emono_abstract(client: httpx2.Client, url: str) -> str | None:
    m = re.search(META_RE.format(name="citation_abstract"), client.get(url).text)
    if not m:
        return None
    text = re.sub(r"\s+", " ", m.group(1)).strip()
    return f"<p>{text}</p>" if text else None


def chapter_fulltext(source: Path, volume: int, suffix: str) -> tuple[str, str | None]:
    html = (source / "html" / f"volume-{volume:02d}" / f"{suffix}.html").read_text("utf-8")
    body = MAIN_RE.search(html)
    fp = re.search(META_RE.format(name="citation_firstpage"), html)
    lp = re.search(META_RE.format(name="citation_lastpage"), html)
    pages = f"{fp.group(1)}-{lp.group(1)}" if fp and lp else None
    return (body.group(1).strip() if body else ""), pages


def build_metadata(
    doi: str,
    attrs: dict,
    description: str,
    *,
    is_volume: bool,
    volume_title: str | None,
    volume_isbn: str | None,
    pages: str | None,
    related: list[RelatedIdentifier],
) -> ZenodoMetadata:
    year = int(attrs["publicationYear"])
    isbn = isbn_of(attrs) or volume_isbn
    lang = {"de": "deu", "en": "eng", "fr": "fra", "it": "ita"}.get(attrs.get("language"))
    return ZenodoMetadata(
        title=attrs["titles"][0]["title"].strip(),
        upload_type="publication",
        publication_type="book" if is_volume else "section",
        description=description,
        creators=creators_of(attrs),
        publication_date=f"{year}-01-01",
        doi=doi,
        license=license_of(attrs),
        language=lang,
        imprint_publisher=PUBLISHER,
        imprint_place=PLACE,
        imprint_isbn=isbn,
        partof_title=None if is_volume else volume_title,
        partof_pages=None if is_volume else pages,
        communities=[],  # handled via the review API, not the metadata field
        related_identifiers=related,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--only", help="enrich just this DOI")
    ap.add_argument("--no-review", action="store_true", help="skip the community review")
    args = ap.parse_args()

    state = json.loads(args.state.read_text("utf-8"))
    dois = list(state)
    chapters_by_volume: dict[int, list[str]] = {}
    for doi in dois:
        _, vol, chap = parse_doi(doi)
        if chap is not None:
            chapters_by_volume.setdefault(vol, []).append(doi)

    settings = Settings()
    with (
        httpx2.Client(timeout=60, follow_redirects=True) as dc,
        ZenodoClient(base_url_for(False), settings.token_for(sandbox=False)) as zc,
    ):
        attrs_by_doi = {doi: datacite(dc, doi) for doi in dois}
        title_by_doi = {d: a["titles"][0]["title"].strip() for d, a in attrs_by_doi.items()}
        isbn_by_volume = {
            vol: isbn_of(attrs_by_doi[doi]) or ISBN_FALLBACK.get(vol)
            for doi in dois
            for _, vol, chap in [parse_doi(doi)]
            if chap is None
        }
        uuid = None if args.no_review else zc.community_uuid(COMMUNITY)

        for doi in dois:
            if args.only and doi != args.only:
                continue
            suffix, vol, chap = parse_doi(doi)
            attrs = attrs_by_doi[doi]
            landing = attrs["url"]
            isbn = isbn_of(attrs) or isbn_by_volume.get(vol)
            alt = []
            if isbn:
                alt.append(
                    RelatedIdentifier(
                        relation="isAlternateIdentifier", identifier=isbn, scheme="isbn"
                    )
                )
            alt.append(
                RelatedIdentifier(
                    relation="isAlternateIdentifier", identifier=landing, scheme="url"
                )
            )

            if chap is None:  # volume
                related = [
                    RelatedIdentifier(
                        relation="hasPart", identifier=c, resource_type="publication-section"
                    )
                    for c in sorted(chapters_by_volume.get(vol, []))
                ] + alt
                description = emono_abstract(dc, landing) or (
                    f"<p>Band {vol} der Buchreihe Stadt.Geschichte.Basel.</p>"
                )
                meta = build_metadata(
                    doi,
                    attrs,
                    description,
                    is_volume=True,
                    volume_title=None,
                    volume_isbn=isbn,
                    pages=None,
                    related=related,
                )
            else:  # chapter
                volume_doi = next(
                    (d for d in dois if parse_doi(d)[1] == vol and parse_doi(d)[2] is None),
                    None,
                )
                fulltext, pages = chapter_fulltext(args.source, vol, suffix)
                related = alt + (
                    [
                        RelatedIdentifier(
                            relation="isPartOf",
                            identifier=volume_doi,
                            resource_type="publication-book",
                        )
                    ]
                    if volume_doi
                    else []
                )
                meta = build_metadata(
                    doi,
                    attrs,
                    fulltext or f"<p>{title_by_doi[doi]}</p>",
                    is_volume=False,
                    volume_title=title_by_doi.get(volume_doi) if volume_doi else None,
                    volume_isbn=isbn,
                    pages=pages,
                    related=related,
                )

            dep_id = state[doi]["deposition_id"]
            zc.update_deposition(dep_id, meta)
            if uuid is not None:
                zc.set_community_review(dep_id, uuid)
            print(f"enriched {doi} (deposition {dep_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
