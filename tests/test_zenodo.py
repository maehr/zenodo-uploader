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


# --- lifecycle verbs, shaped by what sandbox.zenodo.org actually does --------


def _published(client: ZenodoClient, fake: FakeZenodo, tmp_path: Path, doi: str) -> int:
    from zenodo_uploader.models import Creator, ZenodoMetadata

    dep = client.create_deposition(
        ZenodoMetadata(
            title="T",
            upload_type="dataset",
            description="D",
            creators=[Creator(name="Doe, Jane")],
            publication_date="2024-01-01",
            doi=doi,
        )
    )
    blob = tmp_path / "v1.txt"
    blob.write_bytes(b"v1")
    client.upload_file(dep, blob)
    client.publish(dep["id"])
    return int(dep["id"])


def test_is_zenodo_doi() -> None:
    from zenodo_uploader.zenodo import is_zenodo_doi

    assert is_zenodo_doi("10.5281/zenodo.1") is True  # production
    assert is_zenodo_doi("10.5072/zenodo.1") is True  # sandbox
    assert is_zenodo_doi("10.30965/9783657796823") is False
    assert is_zenodo_doi(None) is False


def test_edit_then_discard_round_trip(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    dep_id = _published(client, fake_zenodo, tmp_path, "10.30965/x")
    client.edit_deposition(dep_id)
    assert fake_zenodo.depositions[dep_id]["state"] == "inprogress"
    client.discard_edit(dep_id)
    assert fake_zenodo.depositions[dep_id]["state"] == "done"


def test_edit_unpublished_raises(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    dep = client.create_deposition({"title": "T"})
    with pytest.raises(ZenodoError):
        client.edit_deposition(dep["id"])


def test_discard_error_raises(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    fake_zenodo.fail_next = 400
    with pytest.raises(ZenodoError):
        client.discard_edit(1)


def test_new_version_returns_the_new_draft(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    """Zenodo answers with the new draft, not the record acted on."""
    dep_id = _published(client, fake_zenodo, tmp_path, "10.30965/x")
    draft = client.new_version(dep_id)
    assert draft["id"] != dep_id
    assert draft["state"] == "unsubmitted"
    # The draft inherits the previous version's files.
    assert fake_zenodo.files[draft["id"]] == ["v1.txt"]


def test_new_version_of_unpublished_raises(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    dep = client.create_deposition({"title": "T"})
    with pytest.raises(ZenodoError):
        client.new_version(dep["id"])


def test_list_and_delete_file(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    dep = client.create_deposition({"title": "T"})
    blob = tmp_path / "data.csv"
    blob.write_bytes(b"a,b")
    client.upload_file(dep, blob)

    listed = client.list_files(dep["id"])
    assert [f["filename"] for f in listed] == ["data.csv"]

    # Deletion goes through the file id, because the bucket route answers 404
    # even for a file that is present.
    assert client.delete_file(dep["id"], "data.csv") is True
    assert client.list_files(dep["id"]) == []


def test_delete_absent_file_reports_false(client: ZenodoClient, fake_zenodo: FakeZenodo) -> None:
    dep = client.create_deposition({"title": "T"})
    assert client.delete_file(dep["id"], "nothing.txt") is False


def test_delete_file_error_raises(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status that is neither success nor 'absent' must raise, not be ignored."""
    dep = client.create_deposition({"title": "T"})
    blob = tmp_path / "data.csv"
    blob.write_bytes(b"a,b")
    client.upload_file(dep, blob)

    # 403 is not retried, so it reaches the status check in delete_file.
    monkeypatch.setattr(
        fake_zenodo,
        "_delete_file",
        lambda request: httpx2.Response(403, request=request, json={}),
    )
    with pytest.raises(ZenodoError, match="deleting file"):
        client.delete_file(dep["id"], "data.csv")
