"""Tests for the resumable batch runner."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2

from tests.conftest import FakeZenodo
from zenodo_uploader.batch import (
    ManifestEntry,
    load_manifest,
    load_state,
    process_entry,
    run_batch,
    save_state,
)
from zenodo_uploader.zenodo import ZenodoClient


def _entry(
    tmp_path: Path,
    doi: str = "10.5555/example-chapter",
    community: str | None = None,
) -> ManifestEntry:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    return ManifestEntry(doi=doi, files=[pdf], community=community)


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "doi": "10.1/x",
                    "files": ["a.pdf"],
                    "related": [{"relation": "hasPart", "identifier": "10.1/y"}],
                }
            ]
        )
    )
    entries = load_manifest(manifest)
    assert entries[0].doi == "10.1/x"
    assert entries[0].related[0].relation == "hasPart"


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "state.json"
    save_state(path, {"10.1/x": {"status": "draft"}})
    assert load_state(path) == {"10.1/x": {"status": "draft"}}


def test_process_entry_draft_then_publish(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    entry = _entry(tmp_path)
    with client:
        row = process_entry(client, datacite_client, entry, publish=False)
        assert row["status"] == "draft"
        # Re-running reuses the draft instead of creating a duplicate.
        row = process_entry(client, datacite_client, entry, publish=True)
        assert row["status"] == "published"
        assert len(fake_zenodo.depositions) == 1
        # Once published, mirroring is a no-op.
        row = process_entry(client, datacite_client, entry, publish=True)
        assert row["status"] == "exists"


def test_process_entry_community_draft_attaches_unsubmitted_review(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    """community + no publish: the review is attached but left unsubmitted."""
    entry = _entry(tmp_path, community="my-community")
    with client:
        row = process_entry(client, datacite_client, entry, publish=False)
    assert row["status"] == "draft"
    assert row["community"] == "my-community"
    dep_id = row["deposition_id"]
    assert dep_id in fake_zenodo.reviews  # attached
    assert dep_id not in fake_zenodo.submitted  # but not submitted
    assert dep_id not in fake_zenodo.published


def test_process_entry_community_publish_submits_review(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    """community + publish: the review is submitted, and nothing is published.

    A submitted review is an inclusion request awaiting a curator, so the
    legacy publish action is never called.
    """
    entry = _entry(tmp_path, community="my-community")
    with client:
        row = process_entry(client, datacite_client, entry, publish=True)
    assert row["status"] == "submitted"
    assert row["community"] == "my-community"
    dep_id = row["deposition_id"]
    assert dep_id in fake_zenodo.submitted
    assert dep_id not in fake_zenodo.published


def test_process_entry_reports_existing_record_url(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    entry = _entry(tmp_path)
    with client:
        first = process_entry(client, datacite_client, entry, publish=True)
        again = process_entry(client, datacite_client, entry, publish=True)
    assert first["status"] == "published"
    assert again["status"] == "exists"
    assert again["record_url"] == first["record_url"]


def test_run_batch_skips_submitted_rows(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    """A submitted inclusion request is terminal: a rerun must not touch it."""
    state_path = tmp_path / "state.json"
    entry = _entry(tmp_path, community="my-community")
    with client:
        state = run_batch(client, datacite_client, [entry], state_path, publish=True)
        assert state[entry.key]["status"] == "submitted"
        before = dict(fake_zenodo.depositions)
        state = run_batch(client, datacite_client, [entry], state_path, publish=True)
    assert fake_zenodo.depositions == before


def test_run_batch_resumes_and_records_errors(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    good = _entry(tmp_path)
    bad = ManifestEntry(doi="10.1/broken", files=[tmp_path / "missing.pdf"])
    with client:
        state = run_batch(client, datacite_client, [good, bad], state_path, publish=True)
        assert state[good.key]["status"] == "published"
        assert state[bad.key]["status"] == "error"
        # Resume: published entries are skipped without any new deposition.
        depositions_before = dict(fake_zenodo.depositions)
        state = run_batch(client, datacite_client, [good], state_path, publish=True)
        assert fake_zenodo.depositions == depositions_before
    assert load_state(state_path)[good.key]["status"] == "published"


def test_run_batch_limit_and_draft_skip(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    first = _entry(tmp_path, doi="10.5555/example-chapter")
    second = _entry(tmp_path, doi="10.5555/example-chapter-2")
    with client:
        state = run_batch(client, datacite_client, [first, second], state_path, limit=1)
        assert state[first.key]["status"] == "draft"
        assert second.key not in state
        # Without --publish, existing drafts are left alone.
        state = run_batch(client, datacite_client, [first, second], state_path)
        assert state[second.key]["status"] == "draft"
        assert len(fake_zenodo.depositions) == 2


# --- the general manifest ---------------------------------------------------


def test_manifest_entry_requires_exactly_one_source() -> None:
    import pytest

    with pytest.raises(ValueError, match="exactly one"):
        ManifestEntry()
    with pytest.raises(ValueError, match="exactly one"):
        ManifestEntry(doi="10.1/x", metadata={"title": "T"})


def test_manifest_entry_key_precedence(tmp_path: Path) -> None:
    assert ManifestEntry(doi="10.1/x").key == "10.1/x"
    assert ManifestEntry(id="explicit", doi="10.1/x").key == "explicit"
    assert ManifestEntry(metadata_file=Path("rec.json")).key == "rec.json"


def test_metadata_file_is_read_relative_to_the_manifest(tmp_path: Path) -> None:
    records = tmp_path / "records"
    records.mkdir()
    (records / "ds.json").write_text(json.dumps({"metadata": {"title": "From file"}}))
    entry = ManifestEntry(metadata_file=Path("records/ds.json"))
    assert entry.resolve_metadata(tmp_path) == {"title": "From file"}


def test_absolute_metadata_file_ignores_the_base(tmp_path: Path) -> None:
    """An absolute path is read as it is, with or without a manifest directory."""
    record = tmp_path / "abs.json"
    record.write_text(json.dumps({"title": "Absolute"}))
    entry = ManifestEntry(metadata_file=record)
    assert entry.resolve_metadata() == {"title": "Absolute"}
    assert entry.resolve_metadata(tmp_path / "elsewhere") == {"title": "Absolute"}


def test_load_manifest_rejects_a_duplicate_key(tmp_path: Path) -> None:
    import pytest

    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps([{"doi": "10.1/x"}, {"id": "10.1/x", "metadata": {"title": "T"}}])
    )
    with pytest.raises(ValueError, match="duplicate manifest key"):
        load_manifest(manifest)


def test_process_entry_creates_from_literal_metadata(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    """A metadata entry never touches DataCite or Crossref."""
    blob = tmp_path / "data.csv"
    blob.write_bytes(b"a,b")
    entry = ManifestEntry(
        id="dataset-2024",
        metadata={
            "title": "Dataset",
            "upload_type": "dataset",
            "description": "D",
            "publication_date": "2024-01-01",
            "creators": [{"name": "Doe, Jane"}],
        },
        files=[blob],
    )
    with client:
        row = process_entry(client, datacite_client, entry)
    assert row["status"] == "draft"
    stored = fake_zenodo.depositions[row["deposition_id"]]["metadata"]
    assert stored["title"] == "Dataset"
    assert fake_zenodo.files[row["deposition_id"]] == ["data.csv"]


def test_process_entry_rejects_incomplete_literal_metadata(
    client: ZenodoClient, fake_zenodo: FakeZenodo, datacite_client: httpx2.Client
) -> None:
    import pytest

    entry = ManifestEntry(id="bad", metadata={"title": "T"})
    with client, pytest.raises(ValueError, match="missing required field"):
        process_entry(client, datacite_client, entry)


def test_run_batch_mixes_dois_and_metadata(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    blob = tmp_path / "data.csv"
    blob.write_bytes(b"a,b")
    entries = [
        _entry(tmp_path, doi="10.5555/example-chapter"),
        ManifestEntry(
            id="dataset-2024",
            metadata={
                "title": "Dataset",
                "upload_type": "dataset",
                "description": "D",
                "publication_date": "2024-01-01",
                "creators": [{"name": "Doe, Jane"}],
            },
            files=[blob],
        ),
    ]
    with client:
        state = run_batch(client, datacite_client, entries, tmp_path / "state.json")
    assert set(state) == {"10.5555/example-chapter", "dataset-2024"}
    assert all(row["status"] == "draft" for row in state.values())
