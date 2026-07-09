# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "httpx2>=2.5",
#   "beautifulsoup4>=4.12",
#   "lxml>=5",
# ]
# ///
"""Prepare the Stadt.Geschichte.Basel mirror: collect files, build the manifest.

Covers all 88 DOIs of the book series (9 volumes + 79 chapters). With
``--source`` pointing at a local sgb-minimal-html checkout, the chapter PDFs,
minimal-HTML editions, and official full-volume PDFs are taken from disk.
Without it, chapter files and the official full-volume PDFs are downloaded from
the emono.unibas.ch galleys (and the minimal-HTML editions from GitHub). Each
volume record always attaches the official volume PDF. Writes ``manifest.json``
for ``zenodo-uploader batch``.

Usage:
    uv run mirror_sgb.py [--source /path/to/sgb-minimal-html] [--out .]
    uv run zenodo-uploader batch --manifest manifest.json --state state.json --sandbox
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx2
from bs4 import BeautifulSoup

DOIS_URL = (
    "https://raw.githubusercontent.com/Stadt-Geschichte-Basel/sgb-minimal-html/main/pdf/dois.txt"
)
DATACITE_API = "https://api.datacite.org/dois/"
MINIMAL_HTML_URL = "https://raw.githubusercontent.com/Stadt-Geschichte-Basel/sgb-minimal-html/main/html/volume-{volume:02d}/{suffix}.html"
COMMUNITY = "stadt-geschichte-basel"
DOI_RE = re.compile(r"^10\.21255/(sgb-(\d{2})(?:\.(\d{2}))?-\d+)$")


def parse_doi(doi: str) -> tuple[str, int, int | None]:
    match = DOI_RE.match(doi.strip())
    if not match:
        raise ValueError(f"unexpected DOI shape: {doi}")
    suffix, volume, chapter = match.groups()
    return suffix, int(volume), int(chapter) if chapter is not None else None


def download(client: httpx2.Client, url: str, target: Path) -> None:
    if target.exists():
        return
    response = client.get(url)
    response.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)


def pdf_galley_url(client: httpx2.Client, landing_url: str) -> str:
    """Find the PDF galley on an emono landing page as a direct download URL."""
    response = client.get(landing_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    for anchor in soup.find_all("a", class_="cmp_download_link"):
        if anchor.get_text(strip=True) == "PDF" and anchor.get("href"):
            return str(anchor["href"]).replace("/catalog/view/", "/catalog/download/")
    raise ValueError(f"no PDF galley on {landing_url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("."), help="output directory")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "local sgb-minimal-html checkout; uses pdf/volume-0X/{chapters/}*.pdf "
            "and html/volume-0X/*.html instead of downloading, including the "
            "official full-volume PDFs"
        ),
    )
    parser.add_argument(
        "--pdf-only",
        action="store_true",
        help="attach only the PDF to each chapter record (skip the minimal-HTML edition)",
    )
    args = parser.parse_args()
    files_dir = args.out / "files"

    manifest: list[dict] = []
    volume_dois: dict[int, str] = {}

    with httpx2.Client(timeout=60, follow_redirects=True) as client:
        dois = [line.strip() for line in client.get(DOIS_URL).text.splitlines() if line.strip()]
        for doi in dois:
            suffix, volume, chapter = parse_doi(doi)
            if chapter is None:
                volume_dois[volume] = doi
                continue
            print(f"chapter {doi}", file=sys.stderr)
            if args.source:
                pdf_path = (
                    args.source / "pdf" / f"volume-{volume:02d}" / "chapters" / f"{suffix}.pdf"
                )
                html_path = args.source / "html" / f"volume-{volume:02d}" / f"{suffix}.html"
                if not pdf_path.exists() or (not args.pdf_only and not html_path.exists()):
                    raise FileNotFoundError(f"{pdf_path} or {html_path} missing in --source")
            else:
                attributes = client.get(DATACITE_API + doi).json()["data"]["attributes"]
                landing_url = attributes["url"]
                pdf_path = files_dir / f"volume-{volume:02d}" / f"{suffix}.pdf"
                html_path = files_dir / f"volume-{volume:02d}" / f"{suffix}.html"
                download(client, pdf_galley_url(client, landing_url), pdf_path)
                if not args.pdf_only:
                    download(
                        client, MINIMAL_HTML_URL.format(volume=volume, suffix=suffix), html_path
                    )
            files = [str(pdf_path)] if args.pdf_only else [str(pdf_path), str(html_path)]
            manifest.append({"doi": doi, "files": files, "community": COMMUNITY})

        for volume, doi in sorted(volume_dois.items()):
            suffix, _, _ = parse_doi(doi)
            print(f"volume {doi}", file=sys.stderr)
            if args.source:
                pdf_path = args.source / "pdf" / f"volume-{volume:02d}" / f"{suffix}.pdf"
                if not pdf_path.exists():
                    raise FileNotFoundError(f"official volume PDF missing in --source: {pdf_path}")
            else:
                attributes = client.get(DATACITE_API + doi).json()["data"]["attributes"]
                pdf_path = files_dir / f"volume-{volume:02d}" / f"{suffix}.pdf"
                download(client, pdf_galley_url(client, attributes["url"]), pdf_path)
            manifest.append({"doi": doi, "files": [str(pdf_path)], "community": COMMUNITY})

    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {manifest_path} with {len(manifest)} entries", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
