"""Tests for the Zenodo client against the in-memory fake API."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from tests.conftest import FakeZenodo
from zenodo_uploader.models import Creator, ZenodoMetadata
from zenodo_uploader.zenodo import MAX_ATTEMPTS, ZenodoClient, ZenodoError


def _metadata(doi: str = "10.1/x") -> ZenodoMetadata:
    return ZenodoMetadata(
        title="T",
        upload_type="publication",
        description="D",
        creators=[Creator(name="Doe, Jane")],
        publication_date="2024-01-01",
        doi=doi,
    )


def test_create_upload_publish_roundtrip(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    with client:
        deposition = client.create_deposition(_metadata())
        pdf = tmp_path / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        client.upload_file(deposition, pdf)
        record = client.publish(deposition["id"])
    assert fake_zenodo.files[deposition["id"]] == ["chapter.pdf"]
    assert deposition["id"] in fake_zenodo.published
    assert record["links"]["html"].endswith(str(deposition["id"]))


def test_find_records_and_depositions(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    with client:
        assert client.find_records_by_doi("10.1/x") == []
        deposition = client.create_deposition(_metadata())
        assert client.find_depositions_by_doi("10.1/x")[0]["id"] == deposition["id"]
        pdf = tmp_path / "f.pdf"
        pdf.write_bytes(b"x")
        client.upload_file(deposition, pdf)
        client.publish(deposition["id"])
        assert client.find_records_by_doi("10.1/x")[0]["doi"] == "10.1/x"
        assert client.find_depositions_by_doi("10.1/x") == []


def test_publish_without_files_raises(client: ZenodoClient) -> None:
    with client:
        deposition = client.create_deposition(_metadata())
        with pytest.raises(ZenodoError, match="no files"):
            client.publish(deposition["id"])


def test_delete_draft(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        deposition = client.create_deposition(_metadata())
        client.delete_draft(deposition["id"])
    assert deposition["id"] not in fake_zenodo.depositions


def test_delete_published_fails(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    with client:
        deposition = client.create_deposition(_metadata())
        pdf = tmp_path / "f.pdf"
        pdf.write_bytes(b"x")
        client.upload_file(deposition, pdf)
        client.publish(deposition["id"])
        with pytest.raises(ZenodoError, match="deleting draft"):
            client.delete_draft(deposition["id"])


def test_retry_on_429_then_success(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    fake_zenodo.fail_next = 429
    with client:
        deposition = client.create_deposition(_metadata())
    assert deposition["id"] in fake_zenodo.depositions


def test_gives_up_after_max_attempts() -> None:
    calls = 0

    def always_busy(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(503, request=request, json={})

    client = ZenodoClient(
        "https://zenodo.example",
        "token",
        transport=httpx2.MockTransport(always_busy),
        sleep=lambda _: None,
    )
    with client, pytest.raises(ZenodoError, match="failed after"):
        client.create_deposition(_metadata())
    assert calls == MAX_ATTEMPTS


def test_throttles_between_requests() -> None:
    sleeps: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request, json={"hits": {"hits": []}})

    client = ZenodoClient(
        "https://zenodo.example",
        "token",
        transport=httpx2.MockTransport(handler),
        sleep=sleeps.append,
    )
    with client:
        client.find_records_by_doi("10.1/a")
        client.find_records_by_doi("10.1/b")
    assert sleeps and all(s <= 1.0 for s in sleeps)


def test_error_body_included(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    fake_zenodo.fail_next = 400
    with client, pytest.raises(ZenodoError, match="400"):
        client.create_deposition(_metadata())
