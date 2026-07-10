"""One-page web front for the local-upload intake.

Serves a single drop-a-zip page and streams the POSTed file into
``UPLOAD_DIR`` — the same watched folder the poll loop in ``uploads.py``
consumes, so a browser upload and a Samba/scp drop are literally the same
thing to the pipeline. The body streams to ``UPLOAD_DIR/.incoming/<uuid>``
and is renamed into place only when complete, so the watcher can never grab
a half-received file.

Off by default: the server only starts when ``UPLOAD_HTTP_PORT`` is set.
There is no auth — publish the port to your LAN at most; putting it on the
internet needs a reverse proxy with auth in front.
"""

import logging
import os
import uuid
from urllib.parse import unquote

from aiohttp import web

from config import UPLOAD_DIR, UPLOAD_MAX_TOTAL_BYTES
from uploads import AUDIO_EXTS

logger = logging.getLogger(__name__)

_INCOMING = ".incoming"
_runner: web.AppRunner | None = None

INDEX_HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>music drop</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; font-family: system-ui, sans-serif }
  body { display: grid; place-items: center; min-height: 100dvh; margin: 0;
         background: Canvas; color: CanvasText }
  main { width: min(420px, 92vw); text-align: center }
  h1 { font-size: 1.25rem; font-weight: 600 }
  #drop { border: 2px dashed color-mix(in srgb, CanvasText 30%, transparent);
          border-radius: 14px; padding: 3rem 1rem; cursor: pointer;
          transition: border-color .15s, background .15s }
  #drop.hover { border-color: #7aa2f7;
                background: color-mix(in srgb, #7aa2f7 8%, transparent) }
  #bar { height: 6px; border-radius: 3px; margin-top: 1.2rem; overflow: hidden;
         background: color-mix(in srgb, CanvasText 12%, transparent);
         visibility: hidden }
  #fill { height: 100%; width: 0; background: #7aa2f7; transition: width .1s }
  #msg { min-height: 1.4em; margin-top: .8rem; font-size: .95rem; opacity: .85 }
  small { opacity: .55 }
</style>
<main>
  <h1>&#127925; drop an album</h1>
  <div id="drop">drop a .zip here<br><small>or click to pick a file</small></div>
  <input id="file" type="file" hidden accept=".zip,audio/*">
  <div id="bar"><div id="fill"></div></div>
  <p id="msg"></p>
  <small>tagging &amp; status happen bot-side &mdash; watch Telegram</small>
</main>
<script>
  const drop = document.getElementById('drop'), pick = document.getElementById('file'),
        bar = document.getElementById('bar'), fill = document.getElementById('fill'),
        msg = document.getElementById('msg');
  drop.onclick = () => pick.click();
  pick.onchange = () => pick.files[0] && send(pick.files[0]);
  ['dragover', 'dragenter'].forEach(t => drop.addEventListener(t, e => {
    e.preventDefault(); drop.classList.add('hover');
  }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => {
    e.preventDefault(); drop.classList.remove('hover');
  }));
  drop.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) send(f);
  });
  function send(file) {
    const form = new FormData();
    form.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    bar.style.visibility = 'visible';
    msg.textContent = 'uploading ' + file.name + '…';
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) fill.style.width = (100 * e.loaded / e.total) + '%';
    };
    xhr.onload = () => {
      if (xhr.status === 200) {
        msg.textContent = '✓ received — processing status arrives in Telegram';
        fill.style.width = '100%';
      } else {
        msg.textContent = '✗ ' + (xhr.responseText || xhr.status);
      }
    };
    xhr.onerror = () => { msg.textContent = '✗ upload failed'; };
    xhr.send(form);
  }
</script>
</html>
"""


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def _upload(request: web.Request) -> web.Response:
    if not request.content_type.startswith("multipart/"):
        raise web.HTTPBadRequest(text="multipart form upload expected")
    part = None
    async for p in await request.multipart():
        if p.filename:
            part = p
            break
    if part is None:
        raise web.HTTPBadRequest(text="no file in request")

    # unquote: non-browser clients (aiohttp included) percent-encode the
    # Content-Disposition filename; basename after, so an encoded slash
    # can't reintroduce a path
    name = os.path.basename(unquote(part.filename))
    ext = os.path.splitext(name)[1].lower()
    if not name or (ext != ".zip" and ext not in AUDIO_EXTS):
        raise web.HTTPBadRequest(text="only .zip or a single audio file")

    incoming = os.path.join(UPLOAD_DIR, _INCOMING)
    os.makedirs(incoming, exist_ok=True)
    tmp = os.path.join(incoming, uuid.uuid4().hex)
    received = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await part.read_chunk(1 << 20):
                received += len(chunk)
                if received > UPLOAD_MAX_TOTAL_BYTES:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=UPLOAD_MAX_TOTAL_BYTES, actual_size=received)
                out.write(chunk)
        dest = os.path.join(UPLOAD_DIR, name)
        if os.path.exists(dest):  # same name again — keep both, let intake sort it
            dest = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:6]}-{name}")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    logger.info("Web upload received: %s (%d bytes)", os.path.basename(dest), received)
    return web.json_response({"ok": True, "name": os.path.basename(dest)})


def make_app() -> web.Application:
    # our own streaming cap does the real limiting; client_max_size just must
    # not undercut it (aiohttp's default is 1 MiB)
    app = web.Application(client_max_size=UPLOAD_MAX_TOTAL_BYTES + (1 << 20))
    app.add_routes([web.get("/", _index), web.post("/upload", _upload)])
    return app


async def start(port: int) -> None:
    global _runner
    # partial files from a crashed session would sit in .incoming forever
    incoming = os.path.join(UPLOAD_DIR, _INCOMING)
    if os.path.isdir(incoming):
        for stale in os.listdir(incoming):
            try:
                os.remove(os.path.join(incoming, stale))
            except OSError:
                pass
    _runner = web.AppRunner(make_app())
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    logger.info("Upload page listening on :%d", port)


async def stop() -> None:
    if _runner is not None:
        await _runner.cleanup()
