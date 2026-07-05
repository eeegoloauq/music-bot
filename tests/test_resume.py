"""Journal-driven resume: the accept → journal → report → clear bracket in
the download runners, and the startup driver's cap and ordering rules."""

import types

import pytest

import journal
from journal import PendingDownload

import bot


@pytest.fixture(autouse=True)
def journal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "pending.json"))


class FakeBot:
    def __init__(self):
        self.sent = []      # (chat_id, text)
        self.edited = []    # (chat_id, message_id, text)
        self._next_id = 100

    async def send_message(self, chat_id, text):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return types.SimpleNamespace(message_id=self._next_id, chat_id=chat_id,
                                     edit_text=self._noop, delete=self._noop)

    async def send_photo(self, chat_id, photo, caption):
        raise AssertionError("not used in these tests")

    async def edit_message_text(self, text, chat_id, message_id):
        self.edited.append((chat_id, message_id, text))

    async def _noop(self, *args, **kwargs):
        pass


# --- runner bracket ----------------------------------------------------------

async def test_run_album_clears_journal_after_reported_outcome(monkeypatch):
    async def fake_do(io, status_msg, album_id, force=False):
        assert journal.load()[0].key == ("album", "302127", 42)  # entry live during run

    monkeypatch.setattr(bot, "_do_download_album", fake_do)

    assert await bot._run_album(bot.ChatIO(FakeBot(), 42), "302127") is True
    assert journal.load() == []  # outcome was reported → ledger clean


async def test_run_album_keeps_journal_when_flow_dies(monkeypatch):
    async def fake_do(io, status_msg, album_id, force=False):
        raise RuntimeError("container killed mid-download")

    monkeypatch.setattr(bot, "_do_download_album", fake_do)

    with pytest.raises(RuntimeError):
        await bot._run_album(bot.ChatIO(FakeBot(), 42), "302127", force=True)

    entries = journal.load()
    assert len(entries) == 1
    assert entries[0].key == ("album", "302127", 42)
    assert entries[0].force is True
    assert entries[0].status_message_id == 101  # the message resume will edit


async def test_run_album_reports_duplicates(monkeypatch):
    bot._in_flight.add("album:1")
    try:
        assert await bot._run_album(bot.ChatIO(FakeBot(), 42), "1") is False
    finally:
        bot._in_flight.discard("album:1")
    assert journal.load() == []  # duplicate never journaled


# --- startup driver ----------------------------------------------------------

def _app():
    fake = FakeBot()
    return types.SimpleNamespace(bot=fake), fake


async def test_resume_bumps_edits_and_reruns(monkeypatch):
    calls = []

    async def fake_run_album(io, id, force=False, resume_entry=None):
        calls.append((id, force, resume_entry.resume_attempts))
        journal.remove("album", id, io.chat_id)  # flow reported its outcome
        return True

    monkeypatch.setattr(bot, "_run_album", fake_run_album)
    journal.add(PendingDownload(kind="album", id="1", chat_id=42,
                                status_message_id=7, force=True))
    app, fake = _app()

    await bot._resume_pending(app)

    # bumped before the run; force deliberately not resumed
    assert calls == [("1", False, 1)]
    assert fake.edited == [(42, 7, "🔁 Bot restarted — resuming this download…")]
    assert journal.load() == []


async def test_resume_gives_up_at_attempt_cap(monkeypatch):
    async def must_not_run(*args, **kwargs):
        raise AssertionError("capped entry must not be re-run")

    monkeypatch.setattr(bot, "_run_track", must_not_run)
    journal.add(PendingDownload(kind="track", id="9", chat_id=42,
                                resume_attempts=journal.MAX_RESUME_ATTEMPTS))
    app, fake = _app()

    await bot._resume_pending(app)

    assert journal.load() == []  # dropped, not retried
    assert any("giving up" in text for _, text in fake.sent)


async def test_resume_oldest_first_and_survives_failures(monkeypatch):
    order = []

    async def fake_run_album(io, id, force=False, resume_entry=None):
        order.append(id)
        if id == "old":
            raise RuntimeError("still broken")
        journal.remove("album", id, io.chat_id)
        return True

    monkeypatch.setattr(bot, "_run_album", fake_run_album)
    journal.add(PendingDownload(kind="album", id="new", chat_id=42,
                                requested_at="2026-07-05T12:00:00+00:00"))
    journal.add(PendingDownload(kind="album", id="old", chat_id=42,
                                requested_at="2026-07-05T09:00:00+00:00"))
    app, _ = _app()

    await bot._resume_pending(app)

    assert order == ["old", "new"]  # oldest first; failure didn't stop the loop
    left = journal.load()
    assert [e.id for e in left] == ["old"]  # failed entry kept (bumped) for next boot
    assert left[0].resume_attempts == 1
