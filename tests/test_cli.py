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
        "_datacite_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(fake_datacite)),
    )
    return fake_zenodo


def test_from_doi_dry_run(wired: FakeZenodo) -> None:
    result = runner.invoke(cli.app, ["from-doi", "10.21255/sgb-03.05-238056", "--dry-run"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)["metadata"]
    assert payload["doi"] == "10.21255/sgb-03.05-238056"
    assert not wired.depositions


def test_from_doi_creates_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    result = runner.invoke(
        cli.app,
        [
            "from-doi",
            "10.21255/sgb-03.05-238056",
            "--file",
            str(pdf),
            "--related",
            "isPartOf:10.21255/sgb-03-345800",
            "--sandbox",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "draft"
    assert len(wired.depositions) == 1


def test_from_doi_production_publish_needs_confirmation(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"x")
    args = ["from-doi", "10.21255/sgb-03.05-238056", "--file", str(pdf), "--publish"]
    aborted = runner.invoke(cli.app, args, input="no\n")
    assert aborted.exit_code != 0
    assert not wired.published
    confirmed = runner.invoke(cli.app, args, input="PUBLISH\n")
    assert confirmed.exit_code == 0
    assert wired.published


def test_from_doi_bad_related(wired: FakeZenodo) -> None:
    result = runner.invoke(cli.app, ["from-doi", "10.1/x", "--related", "nocolon", "--sandbox"])
    assert result.exit_code != 0
    result = runner.invoke(
        cli.app, ["from-doi", "10.1/x", "--related", "bogus:10.1/y", "--sandbox"]
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
    dry = runner.invoke(cli.app, ["upload", "--metadata", str(metadata), "--dry-run"])
    assert dry.exit_code == 0
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"x")
    result = runner.invoke(
        cli.app,
        ["upload", "--metadata", str(metadata), "--file", str(pdf), "--sandbox", "--publish"],
    )
    assert result.exit_code == 0
    assert "records" in result.stdout
    draft_only = runner.invoke(cli.app, ["upload", "--metadata", str(metadata), "--sandbox"])
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
        cli.app, ["upload", "--metadata", str(metadata), "--file", str(pdf), "--sandbox"]
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
    result = runner.invoke(cli.app, ["upload", "--metadata", str(metadata), "--sandbox"])
    assert result.exit_code != 0
    assert "description" in result.output
    assert "publication_date" in result.output
    assert not wired.depositions


def test_batch_command(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"doi": "10.21255/sgb-03.05-238056", "files": [str(pdf)]}]))
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
    assert payload == {"doi": "10.1/x", "published": [], "depositions": []}


def test_client_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZENODO_SANDBOX_TOKEN", "sandbox-token")
    with cli._client(sandbox=True) as client:
        assert client.base_url == "https://sandbox.zenodo.org"
    with cli._datacite_client() as datacite_client:
        assert isinstance(datacite_client, httpx2.Client)
