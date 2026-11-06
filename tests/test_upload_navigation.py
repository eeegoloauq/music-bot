"""Upload entry points use the device-reachable URL, independently of providers."""

from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import config

import bot
from flows import common, handlers


@pytest.fixture
def upload_update(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", [1])
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(answer=AsyncMock(), message=message)
    return SimpleNamespace(effective_user=SimpleNamespace(id=1),
                           effective_chat=SimpleNamespace(id=1),
                           message=message, callback_query=query)


@pytest.mark.parametrize("url", ["", "http://music.example.com:8080/upload"])
@pytest.mark.parametrize("handler", [handlers.cmd_upload, handlers._handle_import_help])
async def test_upload_instructions_link_to_page_or_setup(monkeypatch, upload_update, url, handler):
    monkeypatch.setattr(config, "UPLOAD_WEB_URL", url)
    await handler(upload_update, None)
    text = upload_update.message.reply_text.call_args.args[0]
    button = upload_update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == (url or common._UPLOAD_GUIDE)
    assert button.callback_data is None
    assert "here" in text
    assert "watched" not in text
    if handler is handlers._handle_import_help:
        upload_update.callback_query.answer.assert_awaited_once()


@pytest.mark.parametrize("url", ["", "https://music.example.com/import"])
def test_menu_and_provider_fallback_offer_the_same_upload_entry(monkeypatch, upload_update, url):
    monkeypatch.setattr(config, "UPLOAD_WEB_URL", url)
    menu_button = handlers._chat_shortcuts(upload_update).inline_keyboard[0][0]
    fallback_button, providers_button = common._provider_actions().inline_keyboard[0]
    assert menu_button == fallback_button
    if url:
        assert menu_button.url == url
        assert "Upload page" in menu_button.text
    else:
        assert menu_button.callback_data == "import:help"
    assert providers_button.text == "Providers"
    assert providers_button.url.endswith("/tree/main/docs/providers")


@pytest.mark.parametrize("handler", [handlers.cmd_upload, handlers._handle_import_help])
async def test_upload_navigation_requires_authorization(upload_update, handler):
    upload_update.effective_user.id = 2
    await handler(upload_update, None)
    upload_update.message.reply_text.assert_not_awaited()
    upload_update.callback_query.answer.assert_not_awaited()


@pytest.mark.parametrize("url,valid", [
    ("", True),
    (" http://music.example.com:8080/ ", True),
    ("https://music.example.com/import", True),
    ("music.example.com:8080", False),
    ("javascript:alert(1)", False),
    ("https://user:secret@music.example.com", False),
])
def test_upload_url_configuration(monkeypatch, url, valid):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("UPLOAD_WEB_URL", url)
    path = Path(bot.__file__).with_name("config.py")
    if valid:
        config = runpy.run_path(str(path))
        assert config["UPLOAD_WEB_URL"] == url.strip().rstrip("/")
    else:
        with pytest.raises(ValueError, match="UPLOAD_WEB_URL"):
            runpy.run_path(str(path))
