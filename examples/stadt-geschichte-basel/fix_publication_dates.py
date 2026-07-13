"""Correct the publication_date on the Stadt.Geschichte.Basel drafts.

The initial mirror set every record's ``publication_date`` to ``YYYY-01-01``
because DataCite only carries a ``publicationYear``. The real per-volume release
dates are published by Christoph Merian Verlag and exposed on each record's emono
landing page as ``<meta name="citation_publication_date" content="YYYY-MM-DD">``
(volumes 1-4: 2024-03-01, volumes 5-7: 2024-10-01, volumes 8-9: 2025-03-01).

The records are submitted for community review but not yet published, so their
metadata is locked. For each DOI in the batch state file this reuses the proven
withdraw -> edit -> resubmit pattern to change ONLY ``publication_date``:

  - reads the true date from the record's emono landing page,
  - cancels the open community-submission request (unlocking the draft; once a
    review is submitted, DELETE /draft/review no longer works, so the request is
    cancelled via the requests API),
  - PUTs the draft's existing metadata back with the corrected date (files and
    every other field untouched),
  - re-attaches and re-submits the community review.

Idempotent and resumable: a fixed row is marked ``date-fixed`` in the state file,
and a record whose date already matches is skipped. Run from the repo root so the
``.env`` token is found:

    uv run python examples/stadt-geschichte-basel/fix_publication_dates.py \
        --state examples/stadt-geschichte-basel/prod-state.json [--only DOI] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from zenodo_uploader.config import Settings, base_url_for
from zenodo_uploader.zenodo import ZenodoClient

DATACITE_API = "https://api.datacite.org/dois/"
COMMUNITY = "stadt-geschichte-basel"
DATE_META_RE = re.compile(r'<meta name="citation_publication_date"[^>]*content="([^"]*)"', re.I)
DOI_VOLUME_RE = re.compile(r"^10\.21255/sgb-(\d{2})")
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Fallback per-volume release dates (Christoph Merian Verlag), used only when the
# emono landing page is unreachable. Verified against emono citation_publication_date
# for reachable volumes; volumes 1-4 launched together on 2024-03-01.
VOLUME_DATE_FALLBACK = {
    1: "2024-03-01", 2: "2024-03-01", 3: "2024-03-01", 4: "2024-03-01",
    5: "2024-10-01", 6: "2024-10-01", 7: "2024-10-01",
    8: "2025-03-01", 9: "2025-03-01",
}  # fmt: skip


def api(client: httpx2.Client, method: str, url: str, **kwargs: object) -> httpx2.Response:
    """Issue an API request, retrying transient 429/5xx and raising on 4xx.

    emono and Zenodo both return intermittent 500/503s, so every GET/POST goes
    through this retry so a single flaky response does not abort the run.
    """
    resp = None
    for attempt in range(1, 6):
        resp = client.request(method, url, **kwargs)
        if resp.status_code not in RETRY_STATUS:
            break
        time.sleep(min(2**attempt, 30))
    assert resp is not None
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
    return resp


def publication_date_for(client: httpx2.Client, doi: str) -> str:
    """Return the release date for a DOI: emono ``citation_publication_date``,
    falling back to the per-volume date only when emono is unreachable."""
    try:
        landing = api(client, "GET", DATACITE_API + doi).json()["data"]["attributes"]["url"]
        match = DATE_META_RE.search(api(client, "GET", landing).text)
        if match and match.group(1).strip():
            return match.group(1).strip()
        raise ValueError(f"no citation_publication_date on {landing}")
    except (RuntimeError, ValueError) as exc:
        vol_match = DOI_VOLUME_RE.match(doi)
        fallback = VOLUME_DATE_FALLBACK.get(int(vol_match.group(1))) if vol_match else None
        if not fallback:
            raise
        print(f"  emono unavailable for {doi} ({exc}); using volume fallback {fallback}")
        return fallback


def withdraw_submission(client: httpx2.Client, base: str, record_id: int) -> bool:
    """Cancel the open community-submission request so the draft becomes editable.

    Returns True if a request was cancelled, False if none was open (already
    withdrawn), in which case the draft is edited and re-submitted as usual.
    """
    resp = api(
        client, "GET", f"{base}/api/user/requests", params={"q": f"topic.record:{record_id}"}
    )
    for req in resp.json().get("hits", {}).get("hits", []):
        if req.get("type") == "community-submission" and req.get("is_open"):
            api(client, "POST", req["links"]["actions"]["cancel"], json={})
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--only", help="fix just this DOI")
    ap.add_argument("--dry-run", action="store_true", help="print old -> new dates without writing")
    args = ap.parse_args()

    state = json.loads(args.state.read_text("utf-8"))
    settings = Settings()
    base = base_url_for(False)
    token = settings.token_for(sandbox=False)
    with (
        httpx2.Client(timeout=60, follow_redirects=True) as dc,
        httpx2.Client(timeout=60, headers={"Authorization": f"Bearer {token}"}) as rq,
        ZenodoClient(base, token) as zc,
    ):
        community_uuid = None if args.dry_run else zc.community_uuid(COMMUNITY)

        for doi, row in state.items():
            if args.only and doi != args.only:
                continue
            if not args.dry_run and row.get("status") == "date-fixed":
                continue
            dep_id = row["deposition_id"]
            try:
                new_date = publication_date_for(dc, doi)
                metadata = zc.get_deposition(dep_id)["metadata"]
                old_date = metadata.get("publication_date")

                if old_date == new_date:
                    print(f"skip {doi}: already {new_date}")
                    continue
                print(f"{doi}: {old_date} -> {new_date}")
                if args.dry_run:
                    continue

                withdraw_submission(rq, base, dep_id)
                metadata["publication_date"] = new_date
                zc.update_deposition(dep_id, metadata)
                zc.set_community_review(dep_id, community_uuid)
                zc.submit_review(dep_id)
                row["status"] = "date-fixed"
            except Exception as exc:  # record and continue; a rerun retries it
                row["status"] = "date-error"
                row["error"] = str(exc)
                print(f"ERROR {doi}: {exc}")
            if not args.dry_run:
                row["timestamp"] = datetime.now(UTC).isoformat()
                args.state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
