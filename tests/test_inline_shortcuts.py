"""/start and /help shortcut buttons open inline modes the handler knows."""

import types
from unittest.mock import AsyncMock

import pytest

import bot
import inline


def _buttons():
    return [btn for row in bot._inline_shortcuts().inline_keyboard for btn in row]


def test_every_button_prefills_an_inline_query():
    for btn in _buttons():
        data = btn.to_dict()
        assert "switch_inline_query_current_chat" in data or "switch_inline_query" in data, btn.text


@pytest.mark.parametrize("query,handler", [
    ("np", "_np_or_share_or_lyrics"),
    ("l", "_np_or_share_or_lyrics"),
    ("s", "_np_or_share_or_lyrics"),
    ("del ", "_inline_delete"),
    ("", "_inline_hint"),
])
async def test_button_query_reaches_its_mode(monkeypatch, query, handler):
    assert query in [b.switch_inline_query_current_chat for b in _buttons()]
    called = AsyncMock()
    monkeypatch.setattr(inline, handler, called)
    monkeypatch.setattr(inline, "ALLOWED_USERS", [1])
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=1),
                                   inline_query=types.SimpleNamespace(query=query))
    await inline.handle_inline_query(update, None)
    called.assert_awaited_once()


def test_send_to_chat_button_shares_now_playing():
    assert _buttons()[-1].switch_inline_query == "np"


@pytest.mark.parametrize("pinned_by,pins", [(None, 1), (42, 0), (7, 1)])
async def test_start_pins_the_shortcuts_only_once(monkeypatch, pinned_by, pins):
    monkeypatch.setattr(bot, "ALLOWED_USERS", [1])
    msg = types.SimpleNamespace(chat_id=1, pin=AsyncMock())
    pinned = (types.SimpleNamespace(from_user=types.SimpleNamespace(id=pinned_by))
              if pinned_by else None)
    fake_bot = types.SimpleNamespace(
        id=42, get_me=AsyncMock(return_value=types.SimpleNamespace(username="b")),
        get_chat=AsyncMock(return_value=types.SimpleNamespace(pinned_message=pinned)))
    update = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=1),
        message=types.SimpleNamespace(reply_text=AsyncMock(return_value=msg)))
    await bot.cmd_start(update, types.SimpleNamespace(bot=fake_bot, args=[]))
    assert msg.pin.await_count == pins
