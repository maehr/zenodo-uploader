"""CLI tests via Typer's test runner with the fake Zenodo transport."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from typer.testing import CliRunner

from tests.conftest import DATACITE_CHAPTER, FakeZenodo
from zenodo_uploader import cli
from zenodo_uploader.zenodo import ZenodoClient

runner = CliRunner()


def _message(result: object) -> str:
    """Flatten Typer's boxed error output so a wrapped sentence can be matched."""
    text = getattr(result, "output", "")
    for box in "│╭╮╰╯─":
        text = text.replace(box, " ")
    return " ".join(text.split())


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, fake_zenodo: FakeZenodo) -> FakeZenodo:
    """Route CLI HTTP traffic to the fakes (Zenodo and DataCite)."""

    def fake_client(sandbox: bool) -> ZenodoClient:
        return ZenodoClient(
            "https://zenodo.example",
            "token",
            transport=httpx2.MockTransport(fake_zenodo.handler),
            sleep=lambda _: None,
        )

    def fake_datacite(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, request=request, json={"data": {"attributes": DATACITE_CHAPTER}}
        )

    monkeypatch.setattr(cli, "_client", fake_client)
    monkeypatch.setattr(
        cli,
        "_registry_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(fake_datacite)),
    )
    return fake_zenodo


def test_create_from_doi_dry_run(wired: FakeZenodo) -> None:
    result = runner.invoke(
        cli.app, ["create", "--from-doi", "10.5555/example-chapter", "--dry-run"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)["metadata"]
    assert payload["doi"] == "10.5555/example-chapter"
    assert not wired.depositions


def test_create_from_doi_creates_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    result = runner.invoke(
        cli.app,
        [
            "create",
            "--from-doi",
            "10.5555/example-chapter",
            "--file",
            str(pdf),
            "--related",
            "isPartOf:10.5555/example-book",
            "--sandbox",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "draft"
    assert len(wired.depositions) == 1


def test_create_from_doi_production_publish_needs_confirmation(
    wired: FakeZenodo, tmp_path: Path
) -> None:
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"x")
    args = ["create", "--from-doi", "10.5555/example-chapter", "--file", str(pdf), "--publish"]
    aborted = runner.invoke(cli.app, args, input="no\n")
    assert aborted.exit_code != 0
    assert not wired.published
    confirmed = runner.invoke(cli.app, args, input="PUBLISH\n")
    assert confirmed.exit_code == 0
    assert wired.published


def test_create_from_doi_bad_related(wired: FakeZenodo) -> None:
    result = runner.invoke(
        cli.app, ["create", "--from-doi", "10.1/x", "--related", "nocolon", "--sandbox"]
    )
    assert result.exit_code != 0
    result = runner.invoke(
        cli.app, ["create", "--from-doi", "10.1/x", "--related", "bogus:10.1/y", "--sandbox"]
    )
    assert result.exit_code != 0


def test_upload_command(wired: FakeZenodo, tmp_path: Path) -> None:
    metadata = tmp_path / "meta.json"
    metadata.write_text(
        json.dumps(
            {
                "metadata": {
                    "title": "T",
                    "upload_type": "publication",
                    "description": "D",
                    "creators": [{"name": "Doe, Jane"}],
                    "publication_date": "2024-01-01",
                }
            }
        )
    )
    dry = runner.invoke(cli.app, ["create", "--metadata", str(metadata), "--dry-run"])
    assert dry.exit_code == 0
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"x")
    result = runner.invoke(
        cli.app,
        ["create", "--metadata", str(metadata), "--file", str(pdf), "--sandbox", "--publish"],
    )
    assert result.exit_code == 0
    assert "records" in result.stdout
    draft_only = runner.invoke(cli.app, ["create", "--metadata", str(metadata), "--sandbox"])
    assert draft_only.exit_code == 0
    assert "deposit" in draft_only.stdout


def test_upload_zenodo_json_passthrough(wired: FakeZenodo, tmp_path: Path) -> None:
    """A flat .zenodo.json keeps its Zenodo-specific fields verbatim and mints a DOI."""
    metadata = tmp_path / ".zenodo.json"
    metadata.write_text(
        json.dumps(
            {
                "title": "My Software",
                "upload_type": "software",
                "description": "A useful tool.",
                "publication_date": "2024-01-01",
                "creators": [{"name": "Doe, Jane", "orcid": "0000-0002-1825-0097"}],
                "version": "2.0.4",
                "access_right": "open",
                "contributors": [{"name": "Roe, Rick", "type": "Editor"}],
                "communities": [{"identifier": "zenodo"}],
                "grants": [{"id": "10.13039/501100000780::283595"}],
            }
        )
    )
    pdf = tmp_path / "f.zip"
    pdf.write_bytes(b"x")
    result = runner.invoke(
        cli.app, ["create", "--metadata", str(metadata), "--file", str(pdf), "--sandbox"]
    )
    assert result.exit_code == 0
    stored = next(iter(wired.depositions.values()))["metadata"]
    assert stored["version"] == "2.0.4"
    assert stored["access_right"] == "open"
    assert stored["contributors"] == [{"name": "Roe, Rick", "type": "Editor"}]
    assert stored["communities"] == [{"identifier": "zenodo"}]
    assert stored["grants"] == [{"id": "10.13039/501100000780::283595"}]
    assert "doi" not in stored  # no doi field -> Zenodo mints a new one


def test_upload_missing_required_field(wired: FakeZenodo, tmp_path: Path) -> None:
    metadata = tmp_path / ".zenodo.json"
    metadata.write_text(
        json.dumps(
            {
                "title": "My Software",
                "upload_type": "software",
                "creators": [{"name": "Doe, Jane"}],
            }
        )
    )
    result = runner.invoke(cli.app, ["create", "--metadata", str(metadata), "--sandbox"])
    assert result.exit_code != 0
    assert "description" in result.output
    assert "publication_date" in result.output
    assert not wired.depositions


def test_batch_command(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"doi": "10.5555/example-chapter", "files": [str(pdf)]}]))
    state = tmp_path / "state.json"
    dry = runner.invoke(cli.app, ["batch", "--manifest", str(manifest), "--dry-run"])
    assert dry.exit_code == 0
    result = runner.invoke(
        cli.app,
        [
            "batch",
            "--manifest",
            str(manifest),
            "--state",
            str(state),
            "--sandbox",
            "--publish",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"published": 1}


def test_check_command(wired: FakeZenodo) -> None:
    result = runner.invoke(cli.app, ["check", "10.1/x", "--sandbox"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"doi": "10.1/x", "published": [], "drafts": []}


def test_client_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZENODO_SANDBOX_TOKEN", "sandbox-token")
    with cli._client(sandbox=True) as client:
        assert client.base_url == "https://sandbox.zenodo.org"
    with cli._registry_client() as registry:
        assert isinstance(registry, httpx2.Client)


# --- the lifecycle verbs ----------------------------------------------------


def _meta_file(tmp_path: Path, title: str = "T", name: str = "meta.json") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "title": title,
                "upload_type": "dataset",
                "description": "D",
                "creators": [{"name": "Doe, Jane"}],
                "publication_date": "2024-01-01",
            }
        )
    )
    return path


def _draft(wired: FakeZenodo, tmp_path: Path, doi: str | None = None) -> int:
    meta = _meta_file(tmp_path)
    if doi is not None:
        payload = json.loads(meta.read_text())
        payload["doi"] = doi
        meta.write_text(json.dumps(payload))
    blob = tmp_path / "payload.csv"
    blob.write_bytes(b"a,b")
    result = runner.invoke(
        cli.app, ["create", "--metadata", str(meta), "--file", str(blob), "--sandbox"]
    )
    assert result.exit_code == 0, result.output
    return int(json.loads(result.stdout)["deposition_id"])


def test_create_rejects_both_sources_and_neither(wired: FakeZenodo, tmp_path: Path) -> None:
    meta = _meta_file(tmp_path)
    neither = runner.invoke(cli.app, ["create", "--sandbox"])
    assert neither.exit_code != 0
    both = runner.invoke(
        cli.app, ["create", "--metadata", str(meta), "--from-doi", "10.1/x", "--sandbox"]
    )
    assert both.exit_code != 0
    assert not wired.depositions


def test_update_a_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    changed = _meta_file(tmp_path, title="Changed", name="changed.json")

    dry = runner.invoke(cli.app, ["update", str(dep_id), "--metadata", str(changed), "--dry-run"])
    assert dry.exit_code == 0
    assert wired.depositions[dep_id]["metadata"]["title"] == "T"  # untouched

    result = runner.invoke(
        cli.app, ["update", str(dep_id), "--metadata", str(changed), "--sandbox"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "draft updated"
    assert wired.depositions[dep_id]["metadata"]["title"] == "Changed"


def test_update_a_published_record_with_an_external_doi(wired: FakeZenodo, tmp_path: Path) -> None:
    """An external DOI survives the edit, update, publish round trip."""
    dep_id = _draft(wired, tmp_path, doi="10.30965/external")
    runner.invoke(cli.app, ["publish", str(dep_id), "--sandbox"])
    changed = _meta_file(tmp_path, title="Changed", name="changed.json")
    result = runner.invoke(
        cli.app, ["update", str(dep_id), "--metadata", str(changed), "--sandbox"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "republished"


def test_update_refuses_a_zenodo_minted_published_record(wired: FakeZenodo, tmp_path: Path) -> None:
    """Zenodo will not re-publish its own DOI, so the command must not try."""
    dep_id = _draft(wired, tmp_path, doi="10.5281/zenodo.999")
    runner.invoke(cli.app, ["publish", str(dep_id), "--sandbox"])
    changed = _meta_file(tmp_path, title="Changed", name="changed.json")
    result = runner.invoke(
        cli.app, ["update", str(dep_id), "--metadata", str(changed), "--sandbox"]
    )
    assert result.exit_code != 0
    assert "Create a new version" in _message(result)
    assert wired.depositions[dep_id]["metadata"]["title"] == "T"  # nothing changed


def test_update_discards_the_edit_when_publishing_fails(
    wired: FakeZenodo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed re-publish must not leave the record stuck in an edit session."""
    dep_id = _draft(wired, tmp_path, doi="10.30965/external")
    runner.invoke(cli.app, ["publish", str(dep_id), "--sandbox"])
    changed = _meta_file(tmp_path, title="Changed", name="changed.json")

    monkeypatch.setattr(
        wired,
        "_publish",
        lambda request: httpx2.Response(400, request=request, json={"message": "pids.doi"}),
    )
    result = runner.invoke(
        cli.app, ["update", str(dep_id), "--metadata", str(changed), "--sandbox"]
    )
    assert result.exit_code != 0
    assert wired.depositions[dep_id]["state"] == "done"  # discarded, not left inprogress


def test_new_version(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)  # no doi -> Zenodo mints one, with a concept DOI
    runner.invoke(cli.app, ["publish", str(dep_id), "--sandbox"])
    v2 = tmp_path / "v2.csv"
    v2.write_bytes(b"a,b")
    changed = _meta_file(tmp_path, title="Version 2", name="v2.json")

    result = runner.invoke(
        cli.app,
        [
            "new-version",
            str(dep_id),
            "--file",
            str(v2),
            "--metadata",
            str(changed),
            "--sandbox",
            "--publish",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    row = json.loads(result.stdout)
    assert row["status"] == "published"
    assert row["of"] == dep_id
    assert row["id"] != dep_id


def test_new_version_stops_at_a_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    runner.invoke(cli.app, ["publish", str(dep_id), "--sandbox"])
    result = runner.invoke(cli.app, ["new-version", str(dep_id), "--sandbox"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "draft"


def test_publish_needs_confirmation_on_production(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"x")
    runner.invoke(cli.app, ["files", "add", str(dep_id), str(pdf), "--sandbox"])

    aborted = runner.invoke(cli.app, ["publish", str(dep_id)], input="no\n")
    assert aborted.exit_code != 0
    assert dep_id not in wired.published

    ok = runner.invoke(cli.app, ["publish", str(dep_id)], input="PUBLISH\n")
    assert ok.exit_code == 0
    assert dep_id in wired.published


def test_submit_to_a_community(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "submit",
            str(dep_id),
            "--community",
            "my-community",
            "--comment",
            "<p>Hi</p>",
            "--sandbox",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "submitted"
    assert dep_id in wired.submitted


def test_delete_needs_confirmation_on_production(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    aborted = runner.invoke(cli.app, ["delete", str(dep_id)], input="no\n")
    assert aborted.exit_code != 0
    assert dep_id in wired.depositions

    ok = runner.invoke(cli.app, ["delete", str(dep_id)], input="DELETE\n")
    assert ok.exit_code == 0
    assert dep_id not in wired.depositions


def test_files_ls_add_rm(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    one = tmp_path / "one.csv"
    one.write_bytes(b"1")
    two = tmp_path / "two.csv"
    two.write_bytes(b"2")

    added = runner.invoke(cli.app, ["files", "add", str(dep_id), str(one), str(two), "--sandbox"])
    assert added.exit_code == 0
    assert json.loads(added.stdout)["added"] == ["one.csv", "two.csv"]

    listed = runner.invoke(cli.app, ["files", "ls", str(dep_id), "--sandbox"])
    assert [f["filename"] for f in json.loads(listed.stdout)] == [
        "payload.csv",
        "one.csv",
        "two.csv",
    ]

    removed = runner.invoke(
        cli.app, ["files", "rm", str(dep_id), "one.csv", "absent.csv", "--sandbox"]
    )
    assert removed.exit_code == 0
    row = json.loads(removed.stdout)
    assert row["removed"] == ["one.csv"]
    assert row["absent"] == ["absent.csv"]


def test_delete_on_sandbox_skips_the_confirmation(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _draft(wired, tmp_path)
    result = runner.invoke(cli.app, ["delete", str(dep_id), "--sandbox"])
    assert result.exit_code == 0
    assert dep_id not in wired.depositions


def test_batch_dry_run_with_metadata_entries(wired: FakeZenodo, tmp_path: Path) -> None:
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "ds.json").write_text(
        json.dumps(
            {
                "title": "Dataset",
                "upload_type": "dataset",
                "description": "D",
                "publication_date": "2024-01-01",
                "creators": [{"name": "Doe, Jane"}],
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"doi": "10.5555/example-chapter"},
                {"id": "dataset", "metadata_file": "records/ds.json"},
            ]
        )
    )
    result = runner.invoke(cli.app, ["batch", "--manifest", str(manifest), "--dry-run"])
    assert result.exit_code == 0
    assert "dataset" in result.stdout
    assert not wired.depositions


def test_new_version_refuses_a_record_with_an_external_doi(
    wired: FakeZenodo, tmp_path: Path
) -> None:
    """No concept DOI means Zenodo cannot version it; say so before acting."""
    dep_id = _draft(wired, tmp_path, doi="10.30965/external")
    runner.invoke(cli.app, ["publish", str(dep_id), "--sandbox"])
    result = runner.invoke(cli.app, ["new-version", str(dep_id), "--sandbox"])
    assert result.exit_code != 0
    assert "Update the metadata" in _message(result)
    # update and new-version cover opposite cases, so the other one works here.
    changed = _meta_file(tmp_path, title="Changed", name="changed.json")
    ok = runner.invoke(cli.app, ["update", str(dep_id), "--metadata", str(changed), "--sandbox"])
    assert ok.exit_code == 0


def test_mint_doi_reaches_the_real_upload(wired: FakeZenodo) -> None:
    """--mint-doi must drop the DOI on the real path, not only in --dry-run."""
    args = ["create", "--from-doi", "10.5555/example-chapter", "--sandbox"]

    kept = runner.invoke(cli.app, [*args, "--keep-doi"])
    assert kept.exit_code == 0, kept.output
    stored = next(iter(wired.depositions.values()))["metadata"]
    assert stored["doi"] == "10.5555/example-chapter"

    wired.depositions.clear()
    minted = runner.invoke(cli.app, [*args, "--mint-doi"])
    assert minted.exit_code == 0, minted.output
    stored = next(iter(wired.depositions.values()))["metadata"]
    assert "doi" not in stored


def test_dry_run_agrees_with_the_real_upload_about_the_doi(wired: FakeZenodo) -> None:
    """The preview must not promise something the real run will not do."""
    args = ["create", "--from-doi", "10.5555/example-chapter", "--mint-doi", "--sandbox"]
    preview = json.loads(runner.invoke(cli.app, [*args, "--dry-run"]).stdout)["metadata"]
    runner.invoke(cli.app, args)
    stored = next(iter(wired.depositions.values()))["metadata"]
    assert ("doi" in preview) == ("doi" in stored) is False
