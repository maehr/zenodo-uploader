"""Submit the enriched Stadt.Geschichte.Basel drafts to the community.

Runs AFTER enrich_drafts.py has attached the community-submission review to
each draft. For every draft still in ``draft`` state this calls submit-review,
turning it into a pending community-inclusion request for a curator to accept.
Idempotent and resumable: the batch state file is updated to ``submitted`` per
DOI, so re-running skips those already sent.

    uv run python examples/stadt-geschichte-basel/submit_community.py \
        --state examples/stadt-geschichte-basel/prod-state.json [--only DOI]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from zenodo_uploader.config import Settings, base_url_for
from zenodo_uploader.zenodo import ZenodoClient

COMMENT = (
    "<p>Automatischer Import der Open-Access-Publikationen der Buchreihe "
    "Stadt.Geschichte.Basel (Christoph Merian Verlag) mit ihren originalen "
    "DOIs. Die Kuration erfolgt durch das Projektteam.</p>"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--only", help="submit just this DOI")
    args = ap.parse_args()

    state = json.loads(args.state.read_text("utf-8"))
    settings = Settings()
    with ZenodoClient(base_url_for(False), settings.token_for(sandbox=False)) as zc:
        for doi, row in state.items():
            if args.only and doi != args.only:
                continue
            if row.get("status") == "submitted":
                continue
            dep = row["deposition_id"]
            try:
                zc.submit_review(dep, comment=COMMENT)
                row["status"] = "submitted"
                print(f"submitted {doi} ({dep})")
            except Exception as exc:  # record and continue
                row["status"] = "submit-error"
                row["error"] = str(exc)
                print(f"ERROR {doi}: {exc}")
            args.state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for row in state.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("summary:", json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
