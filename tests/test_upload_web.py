"""Web upload endpoint: streaming write, rename-on-complete, caps, filtering."""

import io
import os
import zipfile

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import upload_web


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_web, "UPLOAD_DIR", str(tmp_path))
    c = TestClient(TestServer(upload_web.make_app()))
    await c.start_server()
    yield c
    await c.close()


def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Album/01 - a.flac", b"F" * 50)
    return buf.getvalue()


def form(filename, payload):
    f = FormData()
    f.add_field("file", payload, filename=filename)
    return f


async def test_index_serves_page(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "drop an album" in await resp.text()


async def test_upload_lands_in_watched_dir(client, tmp_path):
    resp = await client.post("/upload", data=form("My Album.zip", zip_bytes()))
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "name": "My Album.zip"}
    assert (tmp_path / "My Album.zip").exists()
    assert not os.listdir(tmp_path / ".incoming")  # tmp file consumed by rename


async def test_duplicate_name_kept_apart(client, tmp_path):
    for _ in range(2):
        resp = await client.post("/upload", data=form("same.zip", zip_bytes()))
        assert resp.status == 200
    zips = [n for n in os.listdir(tmp_path) if n.endswith("same.zip")]
    assert len(zips) == 2


async def test_non_audio_extension_rejected(client, tmp_path):
    resp = await client.post("/upload", data=form("tool.exe", b"MZ"))
    assert resp.status == 400
    assert os.listdir(tmp_path) == []


async def test_no_file_part_rejected(client):
    resp = await client.post("/upload", data=FormData({"note": "hi"}))
    assert resp.status == 400


async def test_size_cap_cuts_stream(client, tmp_path, monkeypatch):
    monkeypatch.setattr(upload_web, "UPLOAD_MAX_TOTAL_BYTES", 100)
    resp = await client.post("/upload", data=form("big.zip", b"x" * 500))
    assert resp.status == 413
    assert not os.listdir(tmp_path / ".incoming")  # partial cleaned up
    assert not (tmp_path / "big.zip").exists()


async def test_loose_audio_accepted(client, tmp_path):
    resp = await client.post("/upload", data=form("song.flac", b"F" * 10))
    assert resp.status == 200
    assert (tmp_path / "song.flac").exists()
