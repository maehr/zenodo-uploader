"""Tests for the shared lifecycle module, independent of any interface."""

from __future__ import annotations

import re
from pathlib import Path

import httpx2
import pytest

from tests.conftest import FakeZenodo, datacite_handler
from zenodo_uploader import operations
from zenodo_uploader.operations import UsageError
from zenodo_uploader.zenodo import ZenodoClient, ZenodoError


@pytest.fixture
def registry() -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(datacite_handler))


def _meta(**over: object) -> dict[str, object]:
    return {
        "title": "T",
        "upload_type": "dataset",
        "description": "D",
        "publication_date": "2024-01-01",
        "creators": [{"name": "Doe, Jane"}],
    } | over


def _published(
    client: ZenodoClient, registry: httpx2.Client, tmp_path: Path, **over: object
) -> int:
    blob = tmp_path / "v1.csv"
    blob.write_bytes(b"a,b")
    row = operations.create(client, registry, metadata=_meta(**over), files=[blob])
    dep_id = int(row["deposition_id"])
    client.publish(dep_id)
    return dep_id


def test_update_without_a_confirmation_hook(
    client: ZenodoClient, fake_zenodo: FakeZenodo, registry: httpx2.Client, tmp_path: Path
) -> None:
    """on_republish is optional: a caller that needs no confirmation omits it."""
    with client:
        dep_id = _published(client, registry, tmp_path, doi="10.30965/external")
        row = operations.update(client, dep_id, _meta(title="Changed"))
    assert row["status"] == "republished"


def test_new_version_rolls_back_its_draft_when_a_later_step_fails(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    registry: httpx2.Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zenodo allows one open new version, so a failed attempt must not wedge it."""
    with client:
        dep_id = _published(client, registry, tmp_path)
        before = set(fake_zenodo.depositions)

        # Fail the file upload, after the draft has been opened.
        monkeypatch.setattr(
            fake_zenodo, "_upload", lambda request: httpx2.Response(500, request=request, json={})
        )
        missing = tmp_path / "v2.csv"
        missing.write_bytes(b"2")
        with pytest.raises(ZenodoError):
            operations.new_version(client, dep_id, files=[missing])

        # The draft it opened is gone again, so the next attempt can proceed.
        assert set(fake_zenodo.depositions) == before


def test_new_version_validates_before_opening_a_draft(
    client: ZenodoClient, fake_zenodo: FakeZenodo, registry: httpx2.Client, tmp_path: Path
) -> None:
    """Bad metadata must be rejected before anything is created."""
    with client:
        dep_id = _published(client, registry, tmp_path)
        before = set(fake_zenodo.depositions)
        with pytest.raises(ValueError, match="missing required field"):
            operations.new_version(client, dep_id, metadata={"title": "T"})
        assert set(fake_zenodo.depositions) == before


def test_parse_related_rejects_an_unknown_relation() -> None:
    with pytest.raises(UsageError, match="invalid related identifier"):
        operations.parse_related({"isBestFriendOf": "10.1/x"})


def test_resolve_failure_names_the_doi() -> None:
    def dead(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("down", request=request)

    with (
        httpx2.Client(transport=httpx2.MockTransport(dead)) as offline,
        pytest.raises(UsageError, match=re.escape("cannot resolve 10.1/x")),
    ):
        operations.preview(offline, "10.1/x")


def test_create_requires_exactly_one_source(
    client: ZenodoClient, fake_zenodo: FakeZenodo, registry: httpx2.Client
) -> None:
    """The rule lives here, so every adapter inherits it."""
    with client:
        with pytest.raises(UsageError, match="exactly one"):
            operations.create(client, registry)
        with pytest.raises(UsageError, match="exactly one"):
            operations.create(client, registry, metadata=_meta(), doi="10.1/x")
    assert fake_zenodo.depositions == {}
