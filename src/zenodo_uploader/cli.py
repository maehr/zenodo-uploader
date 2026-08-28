"""Typer CLI for mirroring DOIs to Zenodo."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import httpx2
import structlog
import typer
from pydantic import ValidationError

from .batch import ManifestEntry, load_manifest, mirror_entry, run_batch
from .config import Settings, base_url_for
from .mapping import work_to_zenodo
from .models import RelatedIdentifier
from .resolve import fetch_work
from .zenodo import ZenodoClient

structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))

app = typer.Typer(help=__doc__, no_args_is_help=True)
log = structlog.get_logger()

SandboxOption = Annotated[
    bool, typer.Option("--sandbox", help="Use sandbox.zenodo.org instead of zenodo.org.")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Print planned metadata; perform no writes.")
]
PublishOption = Annotated[
    bool,
    typer.Option("--publish", help="Publish after upload (otherwise stop at draft)."),
]
YesOption = Annotated[
    bool, typer.Option("--yes", help="Skip the interactive production-publish check.")
]


def _confirm_production_publish(sandbox: bool, publish: bool, yes: bool) -> None:
    """Guard: publishing on production Zenodo is irreversible."""
    if not publish or sandbox or yes:
        return
    typer.echo("Publishing on zenodo.org is PERMANENT: published records cannot be deleted.")
    answer = typer.prompt("Type PUBLISH to continue")
    if answer != "PUBLISH":
        raise typer.Abort()


def _client(sandbox: bool) -> ZenodoClient:
    settings = Settings()
    return ZenodoClient(base_url_for(sandbox), settings.token_for(sandbox))


def _datacite_client() -> httpx2.Client:
    return httpx2.Client(timeout=30, follow_redirects=True)


def _parse_related(values: list[str]) -> list[RelatedIdentifier]:
    """Parse ``relation:identifier`` pairs from the command line."""
    related = []
    for value in values:
        relation, _, identifier = value.partition(":")
        if not identifier:
            raise typer.BadParameter(f"expected RELATION:IDENTIFIER, got {value!r}")
        try:
            related.append(
                RelatedIdentifier.model_validate({"relation": relation, "identifier": identifier})
            )
        except ValidationError as exc:
            raise typer.BadParameter(f"unknown relation {relation!r}") from exc
    return related


@app.command()
def from_doi(
    doi: Annotated[str, typer.Argument(help="DOI to mirror, e.g. 10.5555/example-book.")],
    file: Annotated[
        list[Path] | None, typer.Option("--file", exists=True, help="File(s) to attach.")
    ] = None,
    community: Annotated[
        str | None, typer.Option(help="Zenodo community slug to submit the record to.")
    ] = None,
    description: Annotated[
        str | None, typer.Option(help="Override the synthesized HTML description.")
    ] = None,
    related: Annotated[
        list[str] | None,
        typer.Option("--related", help="Extra related identifier as RELATION:IDENTIFIER."),
    ] = None,
    keep_doi: Annotated[
        bool,
        typer.Option(
            "--keep-doi/--mint-doi",
            help="Reuse the existing DOI (default) or let Zenodo mint a new one.",
        ),
    ] = True,
    sandbox: SandboxOption = False,
    dry_run: DryRunOption = False,
    publish: PublishOption = False,
    yes: YesOption = False,
) -> None:
    """Mirror a single DOI: DataCite metadata in, Zenodo deposition out."""
    entry = ManifestEntry(
        doi=doi,
        files=list(file or []),
        description=description,
        community=community,
        related=_parse_related(related or []),
    )
    if dry_run:
        with _datacite_client() as datacite_client:
            record = fetch_work(datacite_client, doi)
        metadata = work_to_zenodo(
            record,
            community=community,
            description=description,
            extra_related=entry.related or None,
            keep_doi=keep_doi,
        )
        typer.echo(json.dumps(metadata.to_payload(), indent=2, ensure_ascii=False))
        typer.echo(f"# would attach: {[str(p) for p in entry.files]}", err=True)
        return
    _confirm_production_publish(sandbox, publish, yes)
    with _client(sandbox) as client, _datacite_client() as datacite_client:
        row = mirror_entry(client, datacite_client, entry, publish=publish)
    typer.echo(json.dumps(row, indent=2))


@app.command()
def upload(
    metadata: Annotated[
        Path, typer.Option(exists=True, help="JSON file with Zenodo deposit metadata.")
    ],
    file: Annotated[
        list[Path] | None, typer.Option("--file", exists=True, help="File(s) to attach.")
    ] = None,
    sandbox: SandboxOption = False,
    dry_run: DryRunOption = False,
    publish: PublishOption = False,
    yes: YesOption = False,
) -> None:
    """Create a deposition from a raw Zenodo metadata JSON file.

    Accepts a ``.zenodo.json`` (fields at the top level) or the API's
    ``{"metadata": {...}}`` shape. All fields are sent to Zenodo verbatim; omit
    ``doi`` to have Zenodo mint a fresh one, or include it to keep an existing DOI.
    """
    from .models import validate_deposit_metadata

    payload = json.loads(metadata.read_text(encoding="utf-8"))
    meta = payload.get("metadata", payload)
    try:
        validate_deposit_metadata(meta)
    except (ValueError, AttributeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--metadata") from exc
    if dry_run:
        typer.echo(json.dumps({"metadata": meta}, indent=2, ensure_ascii=False))
        return
    _confirm_production_publish(sandbox, publish, yes)
    with _client(sandbox) as client:
        deposition = client.create_deposition(meta)
        for path in file or []:
            client.upload_file(deposition, path)
        if publish:
            record = client.publish(deposition["id"])
            typer.echo(record.get("links", {}).get("html", ""))
        else:
            typer.echo(deposition.get("links", {}).get("html", ""))


@app.command()
def batch(
    manifest: Annotated[
        Path, typer.Option(exists=True, help="JSON manifest: array of {doi, files, ...}.")
    ],
    state: Annotated[Path, typer.Option(help="JSON state file for resumable runs.")] = Path(
        "state.json"
    ),
    limit: Annotated[int | None, typer.Option(help="Process at most N pending entries.")] = None,
    sandbox: SandboxOption = False,
    dry_run: DryRunOption = False,
    publish: PublishOption = False,
    yes: YesOption = False,
) -> None:
    """Mirror every entry of a manifest, resuming from the state file."""
    entries = load_manifest(manifest)
    if dry_run:
        with _datacite_client() as datacite_client:
            for entry in entries:
                record = fetch_work(datacite_client, entry.doi)
                metadata = work_to_zenodo(
                    record,
                    community=entry.community,
                    description=entry.description,
                    extra_related=entry.related or None,
                )
                typer.echo(json.dumps(metadata.to_payload(), indent=2, ensure_ascii=False))
        return
    _confirm_production_publish(sandbox, publish, yes)
    with _client(sandbox) as client, _datacite_client() as datacite_client:
        result = run_batch(client, datacite_client, entries, state, publish=publish, limit=limit)
    counts: dict[str, int] = {}
    for row in result.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    typer.echo(json.dumps(counts, indent=2, sort_keys=True))


@app.command()
def check(
    doi: Annotated[str, typer.Argument(help="DOI to look up on Zenodo.")],
    sandbox: SandboxOption = False,
) -> None:
    """Report whether a DOI already exists on Zenodo (record or own draft)."""
    with _client(sandbox) as client:
        records = client.find_records_by_doi(doi)
        drafts = client.find_depositions_by_doi(doi)
    typer.echo(
        json.dumps(
            {
                "doi": doi,
                "published": [r.get("links", {}).get("html") for r in records],
                "depositions": [d.get("id") for d in drafts],
            },
            indent=2,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    app()
