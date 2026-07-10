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
    mirror_entry,
    resync_entry,
    run_batch,
    run_resync,
    save_state,
)
from zenodo_uploader.zenodo import ZenodoClient


def _submitted_draft(
    client: ZenodoClient, doi: str, filename: str, content: bytes, tmp_path: Path
) -> dict:
    """Create a draft with one file and an outstanding community review."""
    from zenodo_uploader.models import Creator, ZenodoMetadata

    dep = client.create_deposition(
        ZenodoMetadata(
            title="T",
            upload_type="publication",
            description="D",
            creators=[Creator(name="Doe, Jane")],
            publication_date="2024-01-01",
            doi=doi,
        )
    )
    original = tmp_path / filename
    original.write_bytes(content)
    client.upload_file(dep, original)
    client.set_community_review(dep["id"], client.community_uuid("stadt-geschichte-basel"))
    client.submit_review(dep["id"])
    return dep


def _entry(tmp_path: Path, doi: str = "10.21255/sgb-03.05-238056") -> ManifestEntry:
    pdf = tmp_path / "chapter.pdf"
    pdf.write_bytes(b"%PDF")
    return ManifestEntry(doi=doi, files=[pdf], community="stadt-geschichte-basel")


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


def test_mirror_entry_draft_then_publish(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    entry = _entry(tmp_path)
    with client:
        row = mirror_entry(client, datacite_client, entry, publish=False)
        assert row["status"] == "draft"
        # Re-running reuses the draft instead of creating a duplicate.
        row = mirror_entry(client, datacite_client, entry, publish=True)
        assert row["status"] == "published"
        assert len(fake_zenodo.depositions) == 1
        # Once published, mirroring is a no-op.
        row = mirror_entry(client, datacite_client, entry, publish=True)
        assert row["status"] == "exists"


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
        assert state[good.doi]["status"] == "published"
        assert state[bad.doi]["status"] == "error"
        # Resume: published entries are skipped without any new deposition.
        depositions_before = dict(fake_zenodo.depositions)
        state = run_batch(client, datacite_client, [good], state_path, publish=True)
        assert fake_zenodo.depositions == depositions_before
    assert load_state(state_path)[good.doi]["status"] == "published"


def test_run_batch_limit_and_draft_skip(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    first = _entry(tmp_path, doi="10.21255/sgb-03.05-238056")
    second = _entry(tmp_path, doi="10.21255/sgb-03.06-114045")
    with client:
        state = run_batch(client, datacite_client, [first, second], state_path, limit=1)
        assert state[first.doi]["status"] == "draft"
        assert second.doi not in state
        # Without --publish, existing drafts are left alone.
        state = run_batch(client, datacite_client, [first, second], state_path)
        assert state[second.doi]["status"] == "draft"
        assert len(fake_zenodo.depositions) == 2


def _plain_draft(
    client: ZenodoClient, doi: str, filename: str, content: bytes, tmp_path: Path
) -> dict:
    """Create a plain editable draft with one file and no review attached."""
    from zenodo_uploader.models import Creator, ZenodoMetadata

    dep = client.create_deposition(
        ZenodoMetadata(
            title="T",
            upload_type="publication",
            description="D",
            creators=[Creator(name="Doe, Jane")],
            publication_date="2024-01-01",
            doi=doi,
        )
    )
    original = tmp_path / filename
    original.write_bytes(content)
    client.upload_file(dep, original)
    return dep


def test_resync_entry_overwrites_file(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    dep = _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    enhanced = tmp_path / "enhanced" / "chapter.pdf"
    enhanced.parent.mkdir()
    enhanced.write_bytes(b"%PDF enhanced")
    entry = ManifestEntry(doi=doi, files=[enhanced])  # no --submit
    with client:
        row = resync_entry(client, entry)
    assert row["status"] == "resynced"
    assert row["deposition_id"] == dep["id"]
    # Same key: overwritten in place, not duplicated; no review touched.
    assert fake_zenodo.files[dep["id"]] == ["chapter.pdf"]
    assert dep["id"] not in fake_zenodo.submitted


def test_resync_entry_submit_attaches_and_submits_review(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    dep = _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    enhanced = tmp_path / "enhanced" / "chapter.pdf"
    enhanced.parent.mkdir()
    enhanced.write_bytes(b"%PDF enhanced")
    entry = ManifestEntry(doi=doi, files=[enhanced], community="stadt-geschichte-basel")
    with client:
        row = resync_entry(client, entry, submit=True)
    assert row["status"] == "submitted"
    assert fake_zenodo.files[dep["id"]] == ["chapter.pdf"]
    assert dep["id"] in fake_zenodo.submitted


def test_resync_entry_submit_falls_back_to_existing_review(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    dep = _submitted_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    enhanced = tmp_path / "enhanced" / "chapter.pdf"
    enhanced.parent.mkdir()
    enhanced.write_bytes(b"%PDF enhanced")
    entry = ManifestEntry(doi=doi, files=[enhanced])  # no community override
    with client:
        row = resync_entry(client, entry, submit=True)
    assert row["status"] == "submitted"
    # Community recovered from the review already attached to the draft.
    assert dep["id"] in fake_zenodo.submitted


def test_resync_entry_submit_errors_without_community(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    dep = _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    enhanced = tmp_path / "enhanced" / "chapter.pdf"
    enhanced.parent.mkdir()
    enhanced.write_bytes(b"%PDF enhanced")
    entry = ManifestEntry(doi=doi, files=[enhanced])  # no community, no review to fall back to
    with client:
        row = resync_entry(client, entry, submit=True)
    assert row["status"] == "error"
    # Files were overwritten before the review step failed; not submitted.
    assert fake_zenodo.files[dep["id"]] == ["chapter.pdf"]
    assert dep["id"] not in fake_zenodo.submitted


def test_run_resync_skips_published(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    enhanced = tmp_path / "enhanced" / "chapter.pdf"
    enhanced.parent.mkdir()
    enhanced.write_bytes(b"%PDF enhanced")
    entry = ManifestEntry(doi=doi, files=[enhanced])
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        {doi: {"status": "published", "record_url": "https://zenodo.example/records/1"}},
    )
    with client:
        state = run_resync(client, [entry], state_path)
    # A published row is terminal: left untouched, not overwritten to "exists".
    assert state[doi]["status"] == "published"
    assert state[doi]["record_url"] == "https://zenodo.example/records/1"


def test_resync_entry_skips_published(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    dep = _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    fake_zenodo.published.add(dep["id"])
    entry = ManifestEntry(doi=doi, files=[tmp_path / "chapter.pdf"])
    with client:
        row = resync_entry(client, entry)
    assert row["status"] == "exists"


def test_resync_entry_errors_without_draft(client: ZenodoClient, tmp_path: Path) -> None:
    entry = ManifestEntry(doi="10.1/none", files=[tmp_path / "chapter.pdf"])
    with client:
        row = resync_entry(client, entry)
    assert row["status"] == "error"


def test_run_resync_records_errors_and_honours_limit(
    client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path
) -> None:
    doi = "10.21255/sgb-03.05-238056"
    _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    # First entry points at a missing file -> upload raises -> recorded as error.
    broken = ManifestEntry(doi=doi, files=[tmp_path / "missing.pdf"])
    second = ManifestEntry(doi="10.1/other", files=[tmp_path / "missing.pdf"])
    state_path = tmp_path / "state.json"
    with client:
        state = run_resync(client, [broken, second], state_path, limit=1)
    assert state[doi]["status"] == "error"
    assert "10.1/other" not in state  # limit=1 stopped before the second entry


def test_run_resync_resumes(client: ZenodoClient, fake_zenodo: FakeZenodo, tmp_path: Path) -> None:
    doi = "10.21255/sgb-03.05-238056"
    _plain_draft(client, doi, "chapter.pdf", b"original", tmp_path)
    enhanced = tmp_path / "enhanced" / "chapter.pdf"
    enhanced.parent.mkdir()
    enhanced.write_bytes(b"%PDF enhanced")
    entry = ManifestEntry(doi=doi, files=[enhanced])
    state_path = tmp_path / "state.json"
    with client:
        state = run_resync(client, [entry], state_path)
        assert state[doi]["status"] == "resynced"
        # Resume: already-resynced entries are skipped (no second upload).
        dep_id = state[doi]["deposition_id"]
        fake_zenodo.files[dep_id] = []
        run_resync(client, [entry], state_path)
    assert fake_zenodo.files[dep_id] == []
