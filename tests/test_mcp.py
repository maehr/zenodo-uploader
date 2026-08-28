"""Tests for the MCP server: tool wiring, trimming, and the production guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.conftest import DATACITE_CHAPTER, FakeZenodo
from zenodo_uploader import mcp
from zenodo_uploader.zenodo import ZenodoClient


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
) -> FakeZenodo:
    """Point the module's client factories at the in-memory doubles."""

    def _client(sandbox: bool) -> ZenodoClient:
        return ZenodoClient(
            "https://zenodo.example",
            "token",
            transport=httpx2.MockTransport(fake_zenodo.handler),
            sleep=lambda _: None,
        )

    monkeypatch.setattr(mcp, "_client", _client)
    monkeypatch.setattr(mcp, "_registry_client", lambda: datacite_client)
    return fake_zenodo


def test_list_tools_exposes_the_lean_surface() -> None:
    tools = asyncio.run(mcp.server.list_tools())
    assert {t.name for t in tools} == {
        "preview_doi",
        "check_doi",
        "mirror_doi",
        "upload_record",
        "submit_to_community",
        "get_deposition",
    }
    by_name = {t.name: t for t in tools}
    # Read tools must be annotated read-only so hosts can auto-approve them.
    assert by_name["preview_doi"].annotations.read_only_hint is True
    assert by_name["check_doi"].annotations.read_only_hint is True
    assert by_name["get_deposition"].annotations.read_only_hint is True
    assert by_name["mirror_doi"].annotations.read_only_hint is False


def test_preview_doi_maps_without_writing(wired: FakeZenodo) -> None:
    payload = mcp.preview_doi(DATACITE_CHAPTER["doi"])
    assert payload["upload_type"] == "publication"
    assert payload["doi"] == DATACITE_CHAPTER["doi"]
    assert wired.depositions == {}  # nothing was created


def test_preview_doi_reports_an_unresolvable_doi(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither registry knows the DOI, so the tool says so instead of crashing."""

    def missing(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request, json={})

    monkeypatch.setattr(
        mcp, "_registry_client", lambda: httpx2.Client(transport=httpx2.MockTransport(missing))
    )
    with pytest.raises(ToolError, match="cannot resolve"):
        mcp.preview_doi("10.1/nowhere")


def test_check_doi_reports_drafts(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    mcp.mirror_doi(DATACITE_CHAPTER["doi"], files=[str(pdf)])
    result = mcp.check_doi(DATACITE_CHAPTER["doi"])
    assert result["doi"] == DATACITE_CHAPTER["doi"]
    assert len(result["drafts"]) == 1
    assert result["published"] == []


def test_mirror_doi_creates_a_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    row = mcp.mirror_doi(DATACITE_CHAPTER["doi"], files=[str(pdf)])
    assert row["status"] == "draft"
    assert wired.files[row["deposition_id"]] == ["chapter.pdf"]


def test_mirror_doi_with_community_submits_a_request(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    row = mcp.mirror_doi(
        DATACITE_CHAPTER["doi"],
        files=[str(pdf)],
        community="my-community",
        publish=True,
    )
    # A community record is submitted for curation, never published outright.
    assert row["status"] == "submitted"
    assert row["deposition_id"] in wired.submitted
    assert row["deposition_id"] not in wired.published


def test_mirror_doi_passes_related_identifiers(wired: FakeZenodo) -> None:
    row = mcp.mirror_doi(
        DATACITE_CHAPTER["doi"],
        related={"isPartOf": "10.5555/example-book"},
    )
    metadata = wired.depositions[row["deposition_id"]]["metadata"]
    assert {
        "relation": "isPartOf",
        "identifier": "10.5555/example-book",
    } in metadata["related_identifiers"]


def test_mirror_doi_rejects_an_unknown_relation(wired: FakeZenodo) -> None:
    with pytest.raises(ToolError, match="invalid arguments"):
        mcp.mirror_doi(DATACITE_CHAPTER["doi"], related={"isBestFriendOf": "10.1/x"})


def test_mirror_doi_rejects_a_missing_file(wired: FakeZenodo, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="file not found"):
        mcp.mirror_doi(DATACITE_CHAPTER["doi"], files=[str(tmp_path / "absent.pdf")])


def test_mirror_doi_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.mirror_doi(DATACITE_CHAPTER["doi"])


def _metadata() -> dict[str, Any]:
    return {
        "title": "T",
        "upload_type": "software",
        "description": "D",
        "publication_date": "2024-01-01",
        "creators": [{"name": "Doe, Jane"}],
    }


def test_upload_record_creates_and_publishes(wired: FakeZenodo, tmp_path: Path) -> None:
    blob = tmp_path / "dist.zip"
    blob.write_bytes(b"zip")
    result = mcp.upload_record(_metadata(), files=[str(blob)], publish=True)
    assert result["id"] in wired.published
    assert result["url"] == f"https://zenodo.example/records/{result['id']}"


def test_upload_record_accepts_the_wrapped_shape(wired: FakeZenodo) -> None:
    result = mcp.upload_record({"metadata": _metadata()})
    assert result["id"] in wired.depositions


def test_upload_record_rejects_incomplete_metadata(wired: FakeZenodo) -> None:
    with pytest.raises(ToolError, match="missing required field"):
        mcp.upload_record({"title": "T"})


def test_upload_record_rejects_a_missing_file(wired: FakeZenodo, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="file not found"):
        mcp.upload_record(_metadata(), files=[str(tmp_path / "absent.zip")])


def test_upload_record_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.upload_record(_metadata())


def test_submit_to_community(wired: FakeZenodo) -> None:
    created = mcp.upload_record(_metadata())
    result = mcp.submit_to_community(created["id"], "my-community", comment="<p>Please.</p>")
    assert result["status"] == "submitted"
    assert created["id"] in wired.submitted


def test_submit_to_community_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.submit_to_community(1, "my-community")


def test_get_deposition_returns_a_trimmed_view(wired: FakeZenodo) -> None:
    created = mcp.upload_record(_metadata())
    result = mcp.get_deposition(created["id"])
    assert set(result) == {"id", "state", "doi", "title", "url"}
    assert result["title"] == "T"


def test_get_deposition_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.get_deposition(1)


def test_client_factory_targets_the_chosen_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZENODO_SANDBOX_TOKEN", "s")
    monkeypatch.setenv("ZENODO_TOKEN", "p")
    with mcp._client(sandbox=True) as sandbox_client:
        assert sandbox_client.base_url == "https://sandbox.zenodo.org"
    with mcp._client(sandbox=False) as production_client:
        assert production_client.base_url == "https://zenodo.org"


def test_registry_client_follows_redirects() -> None:
    with mcp._registry_client() as registry:
        assert registry.follow_redirects is True


def test_missing_token_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZENODO_SANDBOX_TOKEN", raising=False)
    monkeypatch.setattr(mcp.Settings, "model_config", {"env_file": None, "extra": "ignore"})
    with pytest.raises(ToolError, match="ZENODO_SANDBOX_TOKEN is not set"):
        mcp._client(sandbox=True)


# --- the production publish guard -------------------------------------------


def test_guard_allows_sandbox_and_drafts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    mcp._guard_production(sandbox=True, publish=True, confirm=None)
    mcp._guard_production(sandbox=False, publish=False, confirm=None)


def test_guard_blocks_production_publish_without_the_env_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="ZENODO_ALLOW_PRODUCTION_PUBLISH=1"):
        mcp._guard_production(sandbox=False, publish=True, confirm="PUBLISH")


def test_guard_blocks_production_publish_without_the_confirm_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, "1")
    with pytest.raises(ToolError, match="confirm='PUBLISH'"):
        mcp._guard_production(sandbox=False, publish=True, confirm=None)


def test_guard_passes_when_both_confirmations_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, "1")
    mcp._guard_production(sandbox=False, publish=True, confirm="PUBLISH")


def test_mirror_doi_refuses_an_unconfirmed_production_publish(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="Refusing to publish"):
        mcp.mirror_doi(DATACITE_CHAPTER["doi"], sandbox=False, publish=True)
    assert wired.depositions == {}  # refused before any request went out


def test_upload_record_refuses_an_unconfirmed_production_publish(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="Refusing to publish"):
        mcp.upload_record(_metadata(), sandbox=False, publish=True)
    assert wired.depositions == {}
