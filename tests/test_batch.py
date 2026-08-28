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


def test_mirror_entry_community_draft_attaches_unsubmitted_review(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    """community + no publish: the review is attached but left unsubmitted."""
    entry = _entry(tmp_path, community="my-community")
    with client:
        row = mirror_entry(client, datacite_client, entry, publish=False)
    assert row["status"] == "draft"
    assert row["community"] == "my-community"
    dep_id = row["deposition_id"]
    assert dep_id in fake_zenodo.reviews  # attached
    assert dep_id not in fake_zenodo.submitted  # but not submitted
    assert dep_id not in fake_zenodo.published


def test_mirror_entry_community_publish_submits_review(
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
        row = mirror_entry(client, datacite_client, entry, publish=True)
    assert row["status"] == "submitted"
    assert row["community"] == "my-community"
    dep_id = row["deposition_id"]
    assert dep_id in fake_zenodo.submitted
    assert dep_id not in fake_zenodo.published


def test_mirror_entry_reports_existing_record_url(
    client: ZenodoClient,
    fake_zenodo: FakeZenodo,
    datacite_client: httpx2.Client,
    tmp_path: Path,
) -> None:
    entry = _entry(tmp_path)
    with client:
        first = mirror_entry(client, datacite_client, entry, publish=True)
        again = mirror_entry(client, datacite_client, entry, publish=True)
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
        assert state[entry.doi]["status"] == "submitted"
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
    first = _entry(tmp_path, doi="10.5555/example-chapter")
    second = _entry(tmp_path, doi="10.5555/example-chapter-2")
    with client:
        state = run_batch(client, datacite_client, [first, second], state_path, limit=1)
        assert state[first.doi]["status"] == "draft"
        assert second.doi not in state
        # Without --publish, existing drafts are left alone.
        state = run_batch(client, datacite_client, [first, second], state_path)
        assert state[second.doi]["status"] == "draft"
        assert len(fake_zenodo.depositions) == 2
