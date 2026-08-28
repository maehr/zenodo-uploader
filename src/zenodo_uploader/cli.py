"""Typer CLI for the Zenodo deposition lifecycle."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx2
import structlog
import typer
from pydantic import ValidationError

from . import operations
from .batch import load_manifest, run_batch
from .config import Settings, base_url_for
from .models import RelatedIdentifier
from .operations import UsageError
from .zenodo import ZenodoClient

structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))

app = typer.Typer(help=__doc__, no_args_is_help=True)
files_app = typer.Typer(help="Manage the files of a deposition.", no_args_is_help=True)
app.add_typer(files_app, name="files")

SandboxOption = Annotated[
    bool, typer.Option("--sandbox", help="Use sandbox.zenodo.org instead of zenodo.org.")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Print what would be sent; perform no writes.")
]
PublishOption = Annotated[
    bool,
    typer.Option("--publish", help="Publish after upload (otherwise stop at draft)."),
]
YesOption = Annotated[
    bool, typer.Option("--yes", help="Skip the interactive production confirmation.")
]
FileOption = Annotated[
    list[Path] | None, typer.Option("--file", exists=True, help="File(s) to attach.")
]
DepositionArg = Annotated[int, typer.Argument(help="Deposition id, e.g. 1234567.")]


def _confirm_production_publish(sandbox: bool, publish: bool, yes: bool) -> None:
    """Guard: publishing on production Zenodo is irreversible."""
    if not publish or sandbox or yes:
        return
    typer.echo("Publishing on zenodo.org is PERMANENT: published records cannot be deleted.")
    answer = typer.prompt("Type PUBLISH to continue")
    if answer != "PUBLISH":
        raise typer.Abort()


def _confirm_production_delete(sandbox: bool, yes: bool) -> None:
    """Guard: a deleted draft cannot be recovered."""
    if sandbox or yes:
        return
    typer.echo("Deleting a draft on zenodo.org is PERMANENT: it cannot be recovered.")
    answer = typer.prompt("Type DELETE to continue")
    if answer != "DELETE":
        raise typer.Abort()


def _client(sandbox: bool) -> ZenodoClient:
    settings = Settings()
    return ZenodoClient(base_url_for(sandbox), settings.token_for(sandbox))


def _registry_client() -> httpx2.Client:
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


def _read_metadata(path: Path) -> dict[str, Any]:
    """Read a Zenodo metadata file, turning a bad one into a CLI error."""
    try:
        return operations.read_metadata_file(path)
    except (ValueError, AttributeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--metadata") from exc


@contextmanager
def _usage_errors() -> Iterator[None]:
    """Present a lifecycle precondition failure as a CLI error."""
    try:
        yield
    except (UsageError, AttributeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def create(
    metadata: Annotated[
        Path | None,
        typer.Option("--metadata", exists=True, help="JSON file of Zenodo deposit metadata."),
    ] = None,
    from_doi: Annotated[
        str | None,
        typer.Option("--from-doi", help="Mirror an existing DOI instead of supplying metadata."),
    ] = None,
    file: FileOption = None,
    community: Annotated[
        str | None, typer.Option(help="Community slug to submit the record to.")
    ] = None,
    description: Annotated[
        str | None, typer.Option(help="Override the description (--from-doi only).")
    ] = None,
    related: Annotated[
        list[str] | None,
        typer.Option("--related", help="Extra related identifier as RELATION:IDENTIFIER."),
    ] = None,
    keep_doi: Annotated[
        bool,
        typer.Option(
            "--keep-doi/--mint-doi",
            help="Reuse the mirrored DOI (default) or let Zenodo mint a new one.",
        ),
    ] = True,
    sandbox: SandboxOption = False,
    dry_run: DryRunOption = False,
    publish: PublishOption = False,
    yes: YesOption = False,
) -> None:
    """Create a deposition from your own metadata, or by mirroring a DOI.

    Give exactly one source. With ``--metadata`` every field goes to Zenodo
    unchanged, so a ``.zenodo.json`` works as it is. With ``--from-doi`` the
    metadata comes from DataCite or Crossref and maps onto Zenodo fields, and
    the existing DOI is kept unless you pass ``--mint-doi``.
    """
    if (metadata is None) == (from_doi is None):
        raise typer.BadParameter("give exactly one of --metadata or --from-doi")
    meta = _read_metadata(metadata) if metadata is not None else None
    related_ids = _parse_related(related or [])
    files = list(file or [])

    if dry_run:
        if from_doi is not None:
            with _registry_client() as registry:
                payload = {
                    "metadata": operations.preview(
                        registry, from_doi, description=description, keep_doi=keep_doi
                    )
                }
        else:
            payload = {"metadata": meta}
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        typer.echo(f"# would attach: {[str(p) for p in files]}", err=True)
        return

    _confirm_production_publish(sandbox, publish, yes)
    with _usage_errors(), _client(sandbox) as client, _registry_client() as registry:
        row = operations.create(
            client,
            registry,
            metadata=meta,
            doi=from_doi,
            files=files,
            community=community,
            description=description,
            related=related_ids,
            keep_doi=keep_doi,
            publish=publish,
        )
    typer.echo(json.dumps(row, indent=2))


@app.command()
def update(
    deposition_id: DepositionArg,
    metadata: Annotated[
        Path, typer.Option("--metadata", exists=True, help="JSON file of Zenodo deposit metadata.")
    ],
    sandbox: SandboxOption = False,
    dry_run: DryRunOption = False,
    yes: YesOption = False,
) -> None:
    """Replace the metadata of a deposition.

    On a draft this is a plain update. On a published record it unlocks the
    record, updates it, and publishes it again.

    Caution: a published record whose DOI Zenodo minted cannot be published a
    second time. Zenodo rejects it, so this command stops before it changes
    anything and tells you to use ``new-version`` instead.
    """
    meta = _read_metadata(metadata)
    if dry_run:
        typer.echo(json.dumps({"metadata": meta}, indent=2, ensure_ascii=False))
        return

    with _usage_errors(), _client(sandbox) as client:
        row = operations.update(
            client,
            deposition_id,
            meta,
            on_republish=lambda: _confirm_production_publish(sandbox, True, yes),
        )
    typer.echo(json.dumps(row, indent=2))


@app.command("new-version")
def new_version(
    deposition_id: DepositionArg,
    file: FileOption = None,
    metadata: Annotated[
        Path | None,
        typer.Option("--metadata", exists=True, help="Replace the metadata of the new version."),
    ] = None,
    sandbox: SandboxOption = False,
    publish: PublishOption = False,
    yes: YesOption = False,
) -> None:
    """Open a new version of a published record.

    The new draft inherits the files of the previous version and keeps the
    concept DOI, so the record stays one citable series. Attach changed files
    with ``--file``. Only one unpublished new version can exist at a time.

    Caution: Zenodo versions a record through its concept DOI. A record that
    kept an external DOI has none, so Zenodo refuses to version it. This
    command stops before it changes anything and tells you to use ``update``
    instead. The two commands cover opposite cases.
    """
    _confirm_production_publish(sandbox, publish, yes)
    with _usage_errors(), _client(sandbox) as client:
        row = operations.new_version(
            client,
            deposition_id,
            files=file or [],
            metadata=_read_metadata(metadata) if metadata is not None else None,
            publish=publish,
        )
    typer.echo(json.dumps(row, indent=2))


@app.command()
def publish(
    deposition_id: DepositionArg,
    sandbox: SandboxOption = False,
    yes: YesOption = False,
) -> None:
    """Publish an existing draft."""
    _confirm_production_publish(sandbox, True, yes)
    with _client(sandbox) as client:
        row = operations.publish(client, deposition_id)
    typer.echo(json.dumps(row, indent=2))


@app.command()
def submit(
    deposition_id: DepositionArg,
    community: Annotated[str, typer.Option(help="Community slug to submit the draft to.")],
    comment: Annotated[str, typer.Option(help="HTML note for the curators.")] = "",
    sandbox: SandboxOption = False,
) -> None:
    """Submit a draft to a community for inclusion.

    A curator must accept the request, and that acceptance is what publishes
    the record. While the request is open, Zenodo refuses to publish the draft
    and refuses to delete it.
    """
    with _client(sandbox) as client:
        row = operations.submit_to_community(client, deposition_id, community, comment=comment)
    typer.echo(json.dumps(row, indent=2))


@app.command()
def delete(
    deposition_id: DepositionArg,
    sandbox: SandboxOption = False,
    yes: YesOption = False,
) -> None:
    """Delete a draft. A published record cannot be deleted."""
    _confirm_production_delete(sandbox, yes)
    with _client(sandbox) as client:
        row = operations.delete(client, deposition_id)
    typer.echo(json.dumps(row, indent=2))


@files_app.command("ls")
def files_ls(deposition_id: DepositionArg, sandbox: SandboxOption = False) -> None:
    """List the files of a deposition."""
    with _client(sandbox) as client:
        listed = operations.list_files(client, deposition_id)
    typer.echo(json.dumps(listed, indent=2))


@files_app.command("add")
def files_add(
    deposition_id: DepositionArg,
    file: Annotated[list[Path], typer.Argument(exists=True, help="File(s) to upload.")],
    sandbox: SandboxOption = False,
) -> None:
    """Add files to a deposition, overwriting a file of the same name."""
    with _client(sandbox) as client:
        row = operations.add_files(client, deposition_id, file)
    typer.echo(json.dumps(row, indent=2))


@files_app.command("rm")
def files_rm(
    deposition_id: DepositionArg,
    filename: Annotated[list[str], typer.Argument(help="Name(s) of the file(s) to remove.")],
    sandbox: SandboxOption = False,
) -> None:
    """Remove files from a draft by name."""
    with _client(sandbox) as client:
        row = operations.remove_files(client, deposition_id, filename)
    typer.echo(json.dumps(row, indent=2))


@app.command()
def batch(
    manifest: Annotated[
        Path, typer.Option(exists=True, help="JSON manifest: an array of entries.")
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
    """Create every entry of a manifest, resuming from the state file.

    An entry gives exactly one of ``doi``, ``metadata``, or ``metadata_file``,
    so one manifest can mix mirrored DOIs with records of your own.
    """
    entries = load_manifest(manifest)
    base = manifest.parent
    if dry_run:
        with _registry_client() as registry:
            for entry in entries:
                if entry.doi is not None:
                    payload = {
                        "metadata": operations.preview(
                            registry,
                            entry.doi,
                            description=entry.description,
                            keep_doi=entry.keep_doi,
                        )
                    }
                else:
                    payload = {"metadata": entry.resolve_metadata(base)}
                typer.echo(json.dumps({entry.key: payload}, indent=2, ensure_ascii=False))
        return
    _confirm_production_publish(sandbox, publish, yes)
    with _client(sandbox) as client, _registry_client() as registry:
        result = run_batch(
            client, registry, entries, state, publish=publish, limit=limit, base=base
        )
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
        row = operations.check(client, doi)
    typer.echo(json.dumps(row, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
