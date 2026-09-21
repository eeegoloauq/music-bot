"""✖ Cancel on a running download (docs/cancel.md): the button reaches the
run's task, the run reports the tally and clears its journal entry, the
final message carries no keyboard, and only the requesting chat may cancel."""

import asyncio
import types

import pytest

import journal

import bot


@pytest.fixture(autouse=True)
def journal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "pending.json"))
    bot._active_runs.clear()
    yield
    bot._active_runs.clear()


class FakeBot:
    def __init__(self):
        self.sent = []      # (chat_id, text, reply_markup)
        self.edits = []     # (text, reply_markup)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

        async def edit_text(text, **kwargs):
            self.edits.append((text, kwargs.get("reply_markup")))

        return types.SimpleNamespace(message_id=7, chat_id=chat_id,
                                     edit_text=edit_text, delete=edit_text)


def _tap(run_id: str, chat_id: int, user_id: int = 1):
    """A callback-query update for ``cancel:<run_id>`` from ``chat_id``."""
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit_message_reply_markup(reply_markup=None):
        pass

    query = types.SimpleNamespace(
        data=f"cancel:{run_id}", from_user=types.SimpleNamespace(id=user_id),
        answer=answer, edit_message_reply_markup=edit_message_reply_markup,
        message=types.SimpleNamespace(message_id=7, chat_id=chat_id))
    update = types.SimpleNamespace(
        callback_query=query, effective_chat=types.SimpleNamespace(id=chat_id))
    return update, answers


def _run_id(markup) -> str:
    (button,) = [b for row in markup.inline_keyboard for b in row]
    return button.callback_data.split(":", 1)[1]


async def _start_album(monkeypatch, fake, chat_id=42, total=3):
    """Run an album whose download blocks forever after 1 track landed."""
    started = asyncio.Event()

    async def fake_do(io, status_msg, album_id, force=False):
        run = status_msg.run
        run.total = total
        run.saved_fn = lambda: 1
        started.set()
        await asyncio.sleep(3600)          # the transfer that never finishes

    monkeypatch.setattr(bot, "_do_download_album", fake_do)
    monkeypatch.setattr(bot, "ALLOWED_USERS", {1})
    task = asyncio.create_task(bot._run_album(bot.ChatIO(fake, chat_id), "302127"))
    await asyncio.wait_for(started.wait(), 1)
    return task


async def test_status_message_carries_cancel_button(monkeypatch):
    fake = FakeBot()
    task = await _start_album(monkeypatch, fake)
    run_id = _run_id(fake.sent[0][2])
    assert run_id in bot._active_runs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task                          # shutdown cancel, not the user
    assert len(journal.load()) == 1         # …so the request stays journaled


async def test_cancel_tap_stops_run_clears_journal_and_button(monkeypatch):
    fake = FakeBot()
    task = await _start_album(monkeypatch, fake)
    run_id = _run_id(fake.sent[0][2])

    update, answers = _tap(run_id, chat_id=42)
    await bot._handle_cancel_callback(update, None)
    assert answers == ["Cancelling…"]

    assert await asyncio.wait_for(task, 1) is True   # a reported outcome
    assert journal.load() == []                      # restart won't resurrect it
    assert run_id not in bot._active_runs
    text, markup = fake.edits[-1]
    assert text == "✖ Cancelled — 1/3 tracks were already saved"
    assert markup is None                            # final message: no button


async def test_cancel_from_other_chat_is_refused(monkeypatch):
    fake = FakeBot()
    task = await _start_album(monkeypatch, fake, chat_id=42)
    run_id = _run_id(fake.sent[0][2])

    update, answers = _tap(run_id, chat_id=99)
    await bot._handle_cancel_callback(update, None)
    assert answers == ["Only the chat that requested this can cancel it."]
    assert not task.done()
    assert bot._active_runs[run_id].cancel_requested is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_on_finished_run_is_a_noop(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USERS", {1})
    update, answers = _tap("deadbeef0000", chat_id=42)
    await bot._handle_cancel_callback(update, None)
    assert answers == ["Nothing to cancel — already finished."]


async def test_cancel_during_transfer_stops_slskd_and_cleans_staging(monkeypatch):
    """The downloader side: a cancel landing while a file is transferring
    tells slskd to drop it and removes the partial from staging."""
    from soulseek import downloader

    cancelled, removed = [], []

    async def fake_enqueue(chosen):
        pass

    async def fake_await(username, filename, on_progress=None, on_state=None):
        await asyncio.sleep(3600)

    async def fake_cancel(username, filename, remove=True):
        cancelled.append((username, filename, remove))

    monkeypatch.setattr(downloader, "_ensure_enqueued", fake_enqueue)
    monkeypatch.setattr(downloader, "_await_one_file", fake_await)
    monkeypatch.setattr(downloader.slskd, "cancel_download", fake_cancel)
    monkeypatch.setattr(downloader, "_remove_staging_traces",
                        lambda u, f: removed.append((u, f)))

    chosen = types.SimpleNamespace(username="peer", filename="a\\b.flac", extension="flac")
    task = asyncio.create_task(downloader._download_chosen(
        chosen, {"title": "b", "artist": "x"}, {}, "/nonexistent", None, None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [("peer", "a\\b.flac", True)]
    assert removed == [("peer", "a\\b.flac")]


async def test_cancel_still_lands_when_answer_fails(monkeypatch):
    """A Telegram hiccup on query.answer() must not eat the cancel."""
    from telegram.error import TelegramError

    fake = FakeBot()
    task = await _start_album(monkeypatch, fake)
    run_id = _run_id(fake.sent[0][2])

    update, _answers = _tap(run_id, chat_id=42)

    async def failing_answer(text=None, show_alert=False):
        raise TelegramError("blip")

    update.callback_query.answer = failing_answer
    await bot._handle_cancel_callback(update, None)
    assert await asyncio.wait_for(task, 1) is True
    assert journal.load() == []


async def test_restore_backup_puts_a_single_file_back(tmp_path):
    """Force track re-download stages the old file aside; a cancel restores it."""
    original = tmp_path / "01 Song.flac"
    backup = tmp_path / "01 Song.flac.redownload-backup"
    backup.write_bytes(b"old")
    (tmp_path / "01 Song.flac").write_bytes(b"half-written fresh copy")

    assert await bot._restore_backup(str(backup), str(original)) is True
    assert original.read_bytes() == b"old"
    assert not backup.exists()
    assert await bot._restore_backup(str(backup), str(original)) is False  # nothing left
