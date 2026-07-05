"""The crash ledger: add/load/remove/bump round-trips, dedup by request
identity, and fail-open behavior on missing or corrupt files."""

import json

import pytest

import journal
from journal import PendingDownload


@pytest.fixture(autouse=True)
def journal_path(tmp_path, monkeypatch):
    path = tmp_path / "state" / "pending-downloads.json"  # parent doesn't exist yet
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(path))
    return path


def test_add_load_roundtrip(journal_path):
    entry = PendingDownload(kind="album", id="302127", chat_id=42,
                            status_message_id=7, force=True)
    journal.add(entry)

    loaded = journal.load()
    assert len(loaded) == 1
    assert loaded[0].key == ("album", "302127", 42)
    assert loaded[0].force is True
    assert loaded[0].status_message_id == 7
    assert loaded[0].requested_at  # stamped
    assert journal_path.exists()  # parent dir was created


def test_add_replaces_same_request(journal_path):
    journal.add(PendingDownload(kind="album", id="1", chat_id=42))
    journal.add(PendingDownload(kind="album", id="1", chat_id=42, force=True))
    journal.add(PendingDownload(kind="album", id="1", chat_id=99))  # other chat

    loaded = journal.load()
    assert len(loaded) == 2  # re-request superseded, other chat kept
    mine = next(e for e in loaded if e.chat_id == 42)
    assert mine.force is True


def test_remove_only_matching_entry(journal_path):
    journal.add(PendingDownload(kind="album", id="1", chat_id=42))
    journal.add(PendingDownload(kind="track", id="1", chat_id=42))

    journal.remove("album", "1", 42)

    loaded = journal.load()
    assert len(loaded) == 1
    assert loaded[0].kind == "track"


def test_bump_persists_before_resume_runs(journal_path):
    entry = PendingDownload(kind="album", id="1", chat_id=42)
    journal.add(entry)

    journal.bump_attempts(entry)
    assert entry.resume_attempts == 1
    assert journal.load()[0].resume_attempts == 1  # already on disk

    journal.bump_attempts(entry)
    assert journal.load()[0].resume_attempts == 2


def test_missing_file_loads_empty():
    assert journal.load() == []


def test_corrupt_file_discarded(journal_path):
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text("{not json")
    assert journal.load() == []

    journal_path.write_text(json.dumps([{"unexpected": "shape"}]))
    assert journal.load() == []  # TypeError path: unknown fields

    # and a corrupt journal doesn't block new writes
    journal.add(PendingDownload(kind="album", id="1", chat_id=42))
    assert len(journal.load()) == 1
