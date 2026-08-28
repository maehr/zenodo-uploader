"""Tests for the MCP server: tool wiring, trimming, and the production guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.conftest import DATACITE_CHAPTER, FakeZenodo, datacite_handler
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
    # A fresh client per call: each tool closes the one it opens.
    monkeypatch.setattr(
        mcp,
        "_registry_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(datacite_handler)),
    )
    return fake_zenodo


def test_list_tools_exposes_the_lean_surface() -> None:
    tools = asyncio.run(mcp.server.list_tools())
    assert {t.name for t in tools} == {
        "preview_doi",
        "check_doi",
        "create_record",
        "update_record",
        "add_files",
        "remove_file",
        "publish_record",
        "new_version",
        "delete_draft",
        "list_files",
        "submit_to_community",
        "get_deposition",
    }
    # Read tools must be annotated read-only so hosts can auto-approve them.
    read_only = {}
    for tool in tools:
        assert tool.annotations is not None, f"{tool.name} carries no annotations"
        read_only[tool.name] = tool.annotations.read_only_hint
    assert read_only == {
        "preview_doi": True,
        "check_doi": True,
        "get_deposition": True,
        "list_files": True,
        "create_record": False,
        "update_record": False,
        "add_files": False,
        "remove_file": False,
        "publish_record": False,
        "new_version": False,
        "delete_draft": False,
        "submit_to_community": False,
    }
    # Every write tool can reach an irreversible state, so a host must be told
    # to prompt rather than auto-approve.
    destructive = {
        t.name: t.annotations.read_only_hint is False and t.annotations.destructive_hint
        for t in tools
        if t.annotations is not None and t.annotations.read_only_hint is False
    }
    assert destructive == {
        "create_record": True,
        "update_record": True,
        "add_files": True,
        "remove_file": True,
        "publish_record": True,
        "new_version": True,
        "delete_draft": True,
        "submit_to_community": True,
    }


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
    mcp.create_record(doi=DATACITE_CHAPTER["doi"], files=[str(pdf)])
    result = mcp.check_doi(DATACITE_CHAPTER["doi"])
    assert result["doi"] == DATACITE_CHAPTER["doi"]
    assert len(result["drafts"]) == 1
    assert result["published"] == []


def test_create_record_creates_a_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    row = mcp.create_record(doi=DATACITE_CHAPTER["doi"], files=[str(pdf)])
    assert row["status"] == "draft"
    assert wired.files[row["id"]] == ["chapter.pdf"]


def test_create_record_with_community_submits_a_request(wired: FakeZenodo, tmp_path: Path) -> None:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    row = mcp.create_record(
        doi=DATACITE_CHAPTER["doi"],
        files=[str(pdf)],
        community="my-community",
        publish=True,
    )
    # A community record is submitted for curation, never published outright.
    assert row["status"] == "submitted"
    assert row["id"] in wired.submitted
    assert row["id"] not in wired.published


def test_create_record_passes_related_identifiers(wired: FakeZenodo) -> None:
    row = mcp.create_record(
        doi=DATACITE_CHAPTER["doi"],
        related={"isPartOf": "10.5555/example-book"},
    )
    metadata = wired.depositions[row["id"]]["metadata"]
    assert {
        "relation": "isPartOf",
        "identifier": "10.5555/example-book",
    } in metadata["related_identifiers"]


def test_create_record_rejects_an_unknown_relation(wired: FakeZenodo) -> None:
    with pytest.raises(ToolError, match="invalid arguments"):
        mcp.create_record(doi=DATACITE_CHAPTER["doi"], related={"isBestFriendOf": "10.1/x"})


def test_create_record_rejects_a_missing_file(wired: FakeZenodo, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="file not found"):
        mcp.create_record(doi=DATACITE_CHAPTER["doi"], files=[str(tmp_path / "absent.pdf")])


def test_create_record_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.create_record(doi=DATACITE_CHAPTER["doi"])


def _metadata() -> dict[str, Any]:
    return {
        "title": "T",
        "upload_type": "software",
        "description": "D",
        "publication_date": "2024-01-01",
        "creators": [{"name": "Doe, Jane"}],
    }


def test_create_from_metadata_creates_and_publishes(wired: FakeZenodo, tmp_path: Path) -> None:
    blob = tmp_path / "dist.zip"
    blob.write_bytes(b"zip")
    result = mcp.create_record(metadata=_metadata(), files=[str(blob)], publish=True)
    assert result["id"] in wired.published
    assert result["status"] == "published"
    assert result["record_url"] == f"https://zenodo.example/records/{result['id']}"


def test_create_from_metadata_accepts_the_wrapped_shape(wired: FakeZenodo) -> None:
    result = mcp.create_record(metadata={"metadata": _metadata()})
    assert result["id"] in wired.depositions


def test_create_from_metadata_rejects_incomplete_metadata(wired: FakeZenodo) -> None:
    with pytest.raises(ToolError, match="missing required field"):
        mcp.create_record(metadata={"title": "T"})


def test_create_from_metadata_rejects_a_missing_file(wired: FakeZenodo, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="file not found"):
        mcp.create_record(metadata=_metadata(), files=[str(tmp_path / "absent.zip")])


def test_create_from_metadata_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.create_record(metadata=_metadata())


def test_submit_to_community(wired: FakeZenodo) -> None:
    created = mcp.create_record(metadata=_metadata())
    result = mcp.submit_to_community(created["id"], "my-community", comment="<p>Please.</p>")
    assert result["status"] == "submitted"
    assert created["id"] in wired.submitted


def test_submit_to_community_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.submit_to_community(1, "my-community")


def test_get_deposition_returns_a_trimmed_view(wired: FakeZenodo) -> None:
    created = mcp.create_record(metadata=_metadata())
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


def test_create_record_refuses_an_unconfirmed_production_publish(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="Refusing to publish"):
        mcp.create_record(doi=DATACITE_CHAPTER["doi"], sandbox=False, publish=True)
    assert wired.depositions == {}  # refused before any request went out


def test_create_from_metadata_refuses_an_unconfirmed_production_publish(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="Refusing to publish"):
        mcp.create_record(metadata=_metadata(), sandbox=False, publish=True)
    assert wired.depositions == {}


# --- failures must reach the caller as ToolError, never as a raw exception ---


def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every registry request fail the way a dead network does."""

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("registry unreachable", request=request)

    monkeypatch.setattr(
        mcp, "_registry_client", lambda: httpx2.Client(transport=httpx2.MockTransport(boom))
    )


def test_preview_doi_reports_a_network_failure(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)
    with pytest.raises(ToolError, match="cannot resolve"):
        mcp.preview_doi("10.5555/example-chapter")


def test_create_record_reports_a_network_failure(
    wired: FakeZenodo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)
    with pytest.raises(ToolError):
        mcp.create_record(doi="10.5555/example-chapter")


def test_check_doi_surfaces_a_zenodo_error(wired: FakeZenodo) -> None:
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.check_doi("10.5555/example-chapter")


# --- the lifecycle tools ----------------------------------------------------


def _created(wired: FakeZenodo, tmp_path: Path, doi: str | None = None) -> int:
    meta = _metadata()
    if doi is not None:
        meta["doi"] = doi
    blob = tmp_path / "payload.csv"
    blob.write_bytes(b"a,b")
    row = mcp.create_record(metadata=meta, files=[str(blob)])
    return int(row["id"])


def test_create_record_rejects_both_sources_and_neither(wired: FakeZenodo) -> None:
    with pytest.raises(ToolError, match="exactly one"):
        mcp.create_record()
    with pytest.raises(ToolError, match="exactly one"):
        mcp.create_record(metadata=_metadata(), doi="10.1/x")
    assert wired.depositions == {}


def test_list_files_and_add_and_remove(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    assert [f["filename"] for f in mcp.list_files(dep_id)] == ["payload.csv"]

    extra = tmp_path / "extra.csv"
    extra.write_bytes(b"1")
    added = mcp.add_files(dep_id, [str(extra)])
    assert added["added"] == ["extra.csv"]

    assert mcp.remove_file(dep_id, "payload.csv")["removed"] is True
    assert mcp.remove_file(dep_id, "gone.csv")["removed"] is False
    assert [f["filename"] for f in mcp.list_files(dep_id)] == ["extra.csv"]


def test_add_files_rejects_a_missing_file(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    with pytest.raises(ToolError, match="file not found"):
        mcp.add_files(dep_id, [str(tmp_path / "absent.csv")])


def test_file_tools_surface_zenodo_errors(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    for call in (
        lambda: mcp.list_files(dep_id),
        lambda: mcp.remove_file(dep_id, "payload.csv"),
    ):
        wired.fail_next = 400
        with pytest.raises(ToolError):
            call()
    extra = tmp_path / "extra.csv"
    extra.write_bytes(b"1")
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.add_files(dep_id, [str(extra)])


def test_publish_record(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    result = mcp.publish_record(dep_id)
    assert result["id"] == dep_id
    assert dep_id in wired.published


def test_publish_record_surfaces_a_zenodo_error(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.publish_record(dep_id)


def test_update_record_on_a_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    changed = _metadata() | {"title": "Changed"}
    result = mcp.update_record(dep_id, changed)
    assert result["status"] == "draft"
    assert wired.depositions[dep_id]["metadata"]["title"] == "Changed"


def test_update_record_republishes_an_external_doi(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path, doi="10.30965/external")
    mcp.publish_record(dep_id)
    result = mcp.update_record(dep_id, _metadata() | {"title": "Changed"})
    assert result["status"] == "republished"


def test_update_record_refuses_a_zenodo_minted_doi(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    mcp.publish_record(dep_id)
    with pytest.raises(ToolError, match="new_version"):
        mcp.update_record(dep_id, _metadata() | {"title": "Changed"})


def test_update_record_rejects_incomplete_metadata(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    with pytest.raises(ToolError, match="missing required field"):
        mcp.update_record(dep_id, {"title": "T"})


def test_update_record_discards_the_edit_when_publishing_fails(
    wired: FakeZenodo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep_id = _created(wired, tmp_path, doi="10.30965/external")
    mcp.publish_record(dep_id)
    monkeypatch.setattr(
        wired, "_publish", lambda request: httpx2.Response(400, request=request, json={})
    )
    with pytest.raises(ToolError):
        mcp.update_record(dep_id, _metadata() | {"title": "Changed"})
    assert wired.depositions[dep_id]["state"] == "done"


def test_new_version(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    mcp.publish_record(dep_id)
    v2 = tmp_path / "v2.csv"
    v2.write_bytes(b"2")

    draft = mcp.new_version(dep_id, files=[str(v2)])
    assert draft["of"] == dep_id
    assert draft["id"] != dep_id

    published = mcp.new_version(
        dep_id, files=[str(v2)], metadata=_metadata() | {"title": "V3"}, publish=True
    )
    assert published["of"] == dep_id


def test_new_version_refuses_a_record_without_a_concept_doi(
    wired: FakeZenodo, tmp_path: Path
) -> None:
    dep_id = _created(wired, tmp_path, doi="10.30965/external")
    mcp.publish_record(dep_id)
    with pytest.raises(ToolError, match="update_record"):
        mcp.new_version(dep_id)


def test_new_version_rejects_a_missing_file(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    mcp.publish_record(dep_id)
    with pytest.raises(ToolError, match="file not found"):
        mcp.new_version(dep_id, files=[str(tmp_path / "absent.csv")])


def test_delete_draft(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    assert mcp.delete_draft(dep_id)["status"] == "deleted"
    assert dep_id not in wired.depositions


def test_delete_draft_surfaces_a_zenodo_error(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    mcp.publish_record(dep_id)
    with pytest.raises(ToolError):
        mcp.delete_draft(dep_id)


def test_delete_draft_guard_on_production(
    wired: FakeZenodo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep_id = _created(wired, tmp_path)
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="Refusing to delete"):
        mcp.delete_draft(dep_id, sandbox=False)

    monkeypatch.setenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, "1")
    with pytest.raises(ToolError, match="confirm='DELETE'"):
        mcp.delete_draft(dep_id, sandbox=False)
    assert dep_id in wired.depositions  # refused before any request


def test_publish_and_update_guards_on_production(
    wired: FakeZenodo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep_id = _created(wired, tmp_path)
    monkeypatch.delenv(mcp.ALLOW_PRODUCTION_PUBLISH_ENV, raising=False)
    with pytest.raises(ToolError, match="Refusing to publish"):
        mcp.publish_record(dep_id, sandbox=False)
    with pytest.raises(ToolError, match="Refusing to publish"):
        mcp.new_version(dep_id, sandbox=False, publish=True)


def test_create_record_reports_an_existing_record_without_an_id(
    wired: FakeZenodo, tmp_path: Path
) -> None:
    """An 'exists' row carries no deposition id, so nothing is renamed."""
    dep_id = _created(wired, tmp_path, doi="10.30965/external")
    mcp.publish_record(dep_id)
    again = mcp.create_record(metadata=_metadata() | {"doi": "10.30965/external"})
    assert again["status"] == "exists"
    assert "id" not in again
    assert again["record_url"]


def test_new_version_surfaces_a_zenodo_error(wired: FakeZenodo, tmp_path: Path) -> None:
    dep_id = _created(wired, tmp_path)
    mcp.publish_record(dep_id)
    wired.fail_next = 400
    with pytest.raises(ToolError):
        mcp.new_version(dep_id)
