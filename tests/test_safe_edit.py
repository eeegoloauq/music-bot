"""safe_edit / safe_send: transient Telegram failures on status messages
retry with backoff and never raise (docs/slow-source-recovery.md §B)."""

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

import bot


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Keep the attempt counts, drop the real sleeps."""
    monkeypatch.setattr(bot, "_EDIT_BACKOFF", (0, 0, 0))
    monkeypatch.setattr(bot, "_FINAL_BACKOFF", (0, 0, 0, 0, 0))


class FlakyMessage:
    """edit_text raises ``exc`` for the first ``failures`` calls, then lands."""

    def __init__(self, failures: int, exc: Exception | None = None):
        self.failures = failures
        self.exc = exc or NetworkError("httpx.ReadError")
        self.calls = 0

    async def edit_text(self, text, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return True


async def test_recovers_after_transient_failures():
    msg = FlakyMessage(failures=2)
    assert await bot.safe_edit(msg, "hi") is True
    assert msg.calls == 3


async def test_timed_out_is_transient():
    msg = FlakyMessage(failures=1, exc=TimedOut())
    assert await bot.safe_edit(msg, "hi") is True
    assert msg.calls == 2


async def test_gives_up_without_raising():
    msg = FlakyMessage(failures=99)
    assert await bot.safe_edit(msg, "hi") is False
    assert msg.calls == 4  # initial + 3 backoff retries


async def test_final_retries_harder():
    msg = FlakyMessage(failures=99)
    assert await bot.safe_edit(msg, "hi", final=True) is False
    assert msg.calls == 6  # initial + 5 backoff retries


async def test_flood_wait_honored_once_then_dropped():
    msg = FlakyMessage(failures=1, exc=RetryAfter(0))
    assert await bot.safe_edit(msg, "hi") is True
    assert msg.calls == 2

    msg = FlakyMessage(failures=99, exc=RetryAfter(0))
    assert await bot.safe_edit(msg, "hi") is False
    assert msg.calls == 2  # second flood answer is not argued with


async def test_non_transient_error_not_retried():
    msg = FlakyMessage(failures=99, exc=BadRequest("message is not modified"))
    assert await bot.safe_edit(msg, "hi") is False
    assert msg.calls == 1


async def test_none_message_noops():
    assert await bot.safe_edit(None, "hi") is False


async def test_failed_send_yields_null_message():
    class DeadSender:
        calls = 0

        async def reply_text(self, text):
            DeadSender.calls += 1
            raise NetworkError("down")

    msg = await bot.safe_send(DeadSender(), "hi")
    assert DeadSender.calls == 4  # send used the standard retry ladder
    assert msg.message_id is None
    # the flow keeps using the message; every UI op is a silent no-op
    assert await bot.safe_edit(msg, "later") is False
    await msg.delete()
