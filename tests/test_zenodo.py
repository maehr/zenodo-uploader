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


def test_get_and_update_deposition(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        fetched = client.get_deposition(dep["id"])
        assert fetched["id"] == dep["id"]
        updated = client.update_deposition(
            dep["id"], {"title": "New", "upload_type": "publication"}
        )
        assert updated["metadata"]["title"] == "New"
    assert fake_zenodo.depositions[dep["id"]]["metadata"]["title"] == "New"


def test_update_deposition_with_model(client: ZenodoClient) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        updated = client.update_deposition(dep["id"], _metadata("10.9/z"))
    assert updated["metadata"]["doi"] == "10.9/z"


def test_community_uuid(client: ZenodoClient) -> None:
    with client:
        assert client.community_uuid("some-slug") == "uuid-of-community"


def test_set_review_and_submit(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        client.set_community_review(dep["id"], "uuid-of-community")
        assert fake_zenodo.reviews[dep["id"]]["receiver"] == {"community": "uuid-of-community"}
        assert dep["id"] not in fake_zenodo.submitted
        client.submit_review(dep["id"], comment="Please add")
    assert dep["id"] in fake_zenodo.submitted


def test_submit_review_no_comment(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        client.set_community_review(dep["id"], "uuid-of-community")
        client.submit_review(dep["id"])
    assert dep["id"] in fake_zenodo.submitted


def test_cancel_review(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        client.set_community_review(dep["id"], "uuid-of-community")
        client.submit_review(dep["id"])
        assert dep["id"] in fake_zenodo.submitted
        client.cancel_review(dep["id"])
    assert dep["id"] not in fake_zenodo.submitted
    assert dep["id"] not in fake_zenodo.reviews


def test_cancel_review_without_review_is_noop(client: ZenodoClient) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        # A review-less draft answers DELETE /draft/review with 400; tolerated.
        client.cancel_review(dep["id"])  # must not raise


def test_get_review_returns_attached_review(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        client.set_community_review(dep["id"], "uuid-of-community")
        review = client.get_review(dep["id"])
    assert review is not None
    assert review["receiver"] == {"community": "uuid-of-community"}


def test_get_review_none_on_persistent_500(client: ZenodoClient) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        # A review-less draft answers GET /draft/review with a persistent 500.
        assert client.get_review(dep["id"]) is None


def test_get_review_none_on_error_status(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        client.set_community_review(dep["id"], "uuid-of-community")
        fake_zenodo.fail_next = 404  # non-retryable error resolves to "no review"
        assert client.get_review(dep["id"]) is None


def test_delete_file(client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        client.upload_file(dep, f)
        assert fake_zenodo.files[dep["id"]] == ["a.pdf"]
        client.delete_file(dep, "a.pdf")
    assert fake_zenodo.files[dep["id"]] == []


def test_delete_missing_file_is_noop(client: ZenodoClient) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        client.delete_file(dep, "absent.pdf")  # 404 tolerated


def test_cancel_review_error_raises(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    fake_zenodo.fail_next = 403
    with client, pytest.raises(ZenodoError, match="cancelling review"):
        client.cancel_review(1)


def test_delete_file_error_raises(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    with client:
        dep = client.create_deposition(_metadata())
        fake_zenodo.fail_next = 403
        with pytest.raises(ZenodoError, match="deleting file"):
            client.delete_file(dep, "a.pdf")
