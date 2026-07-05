"""User-facing progress and result rendering for download flows.

The downloader and matcher emit small event dicts (``{"t": ..., ...}``)
through an optional ``on_event`` callback; the classes here fold those
events into HTML text that the bot keeps edited into a single Telegram
status message. The goal: at any moment the user can see what the pipeline
is doing right now — which query is searching, which peer got picked and
why, which track is transferring at what speed — and afterwards gets an
honest per-track reason for anything that didn't land.

Event vocabulary (producers: soulseek.downloader / soulseek.matcher):

  plan            {tracks: [{i, title}], total, skipped}
  search          {query, track?}          — a slskd search started
  search_done     {query, files, peers, track?}
  search_wait     {secs}                   — pacing/backoff delay before next search
  folder          {peer, covered, total, quality, score, alts}
  fallback        {remaining}              — per-track matching phase began
  match           {peer, quality, score, copies}   — single-track pick
  track           {i, state: start|done|fail, title, reason?, fmt?, peer?}
  track_progress  {i, pct, speed_bps, eta}

Unknown events are ignored, so producers can grow without breaking the UI.
"""

import html
import time

# Telegram hard limits: 4096 chars per message, 1024 per photo caption.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

# Above this many tracks the live checklist collapses into a counter line —
# a 100-track compilation would otherwise blow past the message limit.
CHECKLIST_MAX = 28

_TITLE_MAX = 48


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def _trunc(s: str, limit: int = _TITLE_MAX) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def fmt_duration(seconds: float) -> str:
    s = max(int(seconds), 0)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_speed(speed_bps: float) -> str:
    if speed_bps >= 1_048_576:
        return f"{speed_bps / 1_048_576:.1f} MB/s"
    return f"{max(speed_bps, 0) / 1024:.0f} KB/s"


# --- failure reasons ---------------------------------------------------------

# Raw PeerTransferError strings carry slskd state names ("transfer ended in
# state 'Completed, TimedOut'") — map the recognisable ones to plain language.
# Reasons minted by the downloader itself ("nothing found on Soulseek",
# "only lossy copies found (mp3 320 kbps)") pass through unchanged.
_FAILURE_MAP = (
    ("timedout", "peer accepted but never sent the file"),
    ("rejected", "peer rejected the transfer"),
    ("cancelled", "transfer was cancelled"),
    ("errored", "transfer failed on the peer's side"),
    ("refused enqueue", "peer refused the download request"),
    ("could not locate downloaded file", "transfer finished but the file never appeared"),
    ("already failed this album", "every matching peer had already failed on this album"),
    ("no peer returned a usable file", "all matching peers failed"),
)


def humanize_failure(raw: str) -> str:
    low = (raw or "").lower()
    for needle, text in _FAILURE_MAP:
        if needle in low:
            return text
    return raw or "unknown error"


# --- live progress: album ----------------------------------------------------

class AlbumProgress:
    """Folds downloader events into the live status text for an album."""

    def __init__(self, artist: str, title: str, n_tracks: int):
        self.artist = artist
        self.title = title
        self.n_tracks = n_tracks
        self.phase = "search"           # search → download
        self.searches: list[dict] = []  # recent queries, oldest first
        self.wait_until: float | None = None
        self.folder: dict | None = None
        self.fallback_remaining: int | None = None
        self.skipped = 0
        self.order: list[int] = []
        self.tracks: dict[int, dict] = {}
        self.total_dl = 0
        self.started = time.monotonic()

    # Returns True when the change deserves an immediate re-render (phase
    # transitions); False for high-frequency updates the caller may throttle.
    def handle(self, ev: dict) -> bool:
        t = ev.get("t")
        if t == "plan":
            self.skipped = ev.get("skipped", 0)
            self.total_dl = len(ev.get("tracks", []))
            self.order = []
            self.tracks = {}
            for item in ev.get("tracks", []):
                i = item["i"]
                self.order.append(i)
                self.tracks[i] = {"title": item["title"], "state": "pending",
                                  "pct": 0.0, "speed": 0, "reason": None}
            return True
        if t == "search":
            self.wait_until = None
            self.searches.append({"query": ev.get("query", ""),
                                  "track": ev.get("track"), "done": False,
                                  "files": 0, "peers": 0})
            del self.searches[:-4]
            return True
        if t == "search_done":
            self.wait_until = None
            for rec in reversed(self.searches):
                if rec["query"] == ev.get("query") and not rec["done"]:
                    rec.update(done=True, files=ev.get("files", 0),
                               peers=ev.get("peers", 0))
                    break
            return True
        if t == "search_wait":
            self.wait_until = time.monotonic() + float(ev.get("secs", 0))
            return True
        if t == "folder":
            self.folder = ev
            self.phase = "download"
            return True
        if t == "fallback":
            self.fallback_remaining = ev.get("remaining")
            self.phase = "download"
            return True
        if t == "track":
            self.phase = "download"
            rec = self.tracks.get(ev.get("i"))
            if rec is None:
                return True
            state = ev.get("state")
            if state == "start":
                rec.update(state="active", pct=0.0, speed=0, reason=None)
            elif state == "done":
                rec.update(state="done", reason=None)
            elif state == "fail":
                rec.update(state="fail", reason=ev.get("reason"))
            return True
        if t == "track_progress":
            rec = self.tracks.get(ev.get("i"))
            if rec is not None and rec["state"] == "active":
                rec["pct"] = ev.get("pct", 0) or 0
                rec["speed"] = ev.get("speed_bps", 0) or 0
            return False
        return False

    # -- render helpers --

    def _search_lines(self) -> list[str]:
        lines = []
        for rec in self.searches:
            q = _trunc(rec["query"], 40)
            if rec["done"]:
                if rec["peers"]:
                    status = f"{rec['files']} files / {rec['peers']} peers"
                else:
                    status = "nothing"
            else:
                status = "searching…"
            prefix = ""
            if rec.get("track"):
                prefix = f"{esc(_trunc(rec['track'], 24))}: "
            lines.append(f"  {prefix}«{esc(q)}» — {status}")
        wait = (self.wait_until or 0) - time.monotonic()
        if wait > 1:
            lines.append(f"  ⏸ Soulseek pacing — next search in ~{int(wait)}s")
        return lines

    def _counts(self) -> tuple[int, int, int]:
        done = sum(1 for r in self.tracks.values() if r["state"] == "done")
        failed = sum(1 for r in self.tracks.values() if r["state"] == "fail")
        active = sum(1 for r in self.tracks.values() if r["state"] == "active")
        return done, failed, active

    def _eta_line(self) -> str:
        done, failed, _ = self._counts()
        finished = done + failed
        parts = [f"{done}/{self.total_dl} saved"]
        if failed:
            parts.append(f"{failed} failed")
        if 0 < finished < self.total_dl:
            elapsed = time.monotonic() - self.started
            eta = elapsed / finished * (self.total_dl - finished)
            if eta >= 5:
                parts.append(f"~{fmt_duration(eta)} left")
        return " · ".join(parts)

    def _track_line(self, i: int) -> str:
        rec = self.tracks[i]
        title = esc(_trunc(rec["title"]))
        state = rec["state"]
        if state == "done":
            return f"✅ {title}"
        if state == "active":
            extra = ""
            if rec["speed"]:
                extra = f" · {rec['pct']:.0f}% · {fmt_speed(rec['speed'])}"
            return f"⬇️ {title}{extra}"
        if state == "fail":
            reason = humanize_failure(rec["reason"] or "")
            return f"❌ {title} — {esc(reason)}"
        return f"▫️ {title}"

    def render(self) -> str:
        header = f"<b>{esc(self.artist)} — {esc(self.title)}</b>"

        if self.phase == "search":
            out = [f"🔎 {header} · {self.n_tracks} tracks",
                   "Searching Soulseek…"]
            out += self._search_lines()
            return "\n".join(out)[:MESSAGE_LIMIT]

        out = [f"⬇️ {header}"]
        if self.folder:
            f = self.folder
            out.append(
                f"📀 {esc(f.get('peer', '?'))} · {f.get('covered')}/{f.get('total')}"
                f" tracks · {esc(f.get('quality', ''))}"
                f" · score {f.get('score', 0):.0f}"
            )
        elif self.fallback_remaining is not None:
            out.append("🧩 no full-album peer — matching track by track")
        if self.skipped:
            out.append(f"⏭ {self.skipped} already in library")

        # An in-flight fallback search (per-track phase) shows as activity.
        if self.searches and not self.searches[-1]["done"]:
            rec = self.searches[-1]
            who = f" for {esc(_trunc(rec['track'], 30))}" if rec.get("track") else ""
            out.append(f"🔎 searching{who}…")
        wait = (self.wait_until or 0) - time.monotonic()
        if wait > 1:
            out.append(f"⏸ Soulseek pacing — ~{int(wait)}s")

        out.append("")
        if self.total_dl <= CHECKLIST_MAX:
            out += [self._track_line(i) for i in self.order]
        else:
            done, failed, _ = self._counts()
            active = [i for i in self.order if self.tracks[i]["state"] == "active"]
            out.append(f"✅ {done} done · ❌ {failed} failed · "
                       f"▫️ {self.total_dl - done - failed - len(active)} queued")
            out += [self._track_line(i) for i in active]
        out.append("")
        out.append(self._eta_line())
        return "\n".join(out)[:MESSAGE_LIMIT]


# --- live progress: single track ----------------------------------------------

class TrackProgress:
    """Folds downloader events into the live status text for one track."""

    def __init__(self, artist: str, title: str):
        self.artist = artist
        self.title = title
        self.searches: list[dict] = []
        self.wait_until: float | None = None
        self.match: dict | None = None
        self.pct = 0.0
        self.speed = 0
        self.peer: str | None = None
        self.fmt: str | None = None
        self.started = time.monotonic()

    def handle(self, ev: dict) -> bool:
        t = ev.get("t")
        if t == "search":
            self.wait_until = None
            self.searches.append({"query": ev.get("query", ""), "done": False,
                                  "files": 0, "peers": 0})
            del self.searches[:-4]
            return True
        if t == "search_done":
            self.wait_until = None
            for rec in reversed(self.searches):
                if rec["query"] == ev.get("query") and not rec["done"]:
                    rec.update(done=True, files=ev.get("files", 0),
                               peers=ev.get("peers", 0))
                    break
            return True
        if t == "search_wait":
            self.wait_until = time.monotonic() + float(ev.get("secs", 0))
            return True
        if t == "match":
            self.match = ev
            return True
        if t == "track":
            if ev.get("state") == "done":
                self.peer = ev.get("peer")
                self.fmt = ev.get("fmt")
            elif ev.get("state") == "start":
                self.pct, self.speed = 0.0, 0
            return True
        if t == "track_progress":
            self.pct = ev.get("pct", 0) or 0
            self.speed = ev.get("speed_bps", 0) or 0
            return False
        return False

    def render(self) -> str:
        header = f"<b>{esc(self.artist)} — {esc(self.title)}</b>"
        if self.match is None:
            out = [f"🔎 {header}", "Searching Soulseek…"]
            for rec in self.searches:
                status = (f"{rec['files']} files / {rec['peers']} peers"
                          if rec["done"] and rec["peers"] else
                          "nothing" if rec["done"] else "searching…")
                out.append(f"  «{esc(_trunc(rec['query'], 40))}» — {status}")
            wait = (self.wait_until or 0) - time.monotonic()
            if wait > 1:
                out.append(f"  ⏸ Soulseek pacing — next search in ~{int(wait)}s")
            return "\n".join(out)[:MESSAGE_LIMIT]

        m = self.match
        out = [f"⬇️ {header}"]
        line = f"🎯 {esc(m.get('peer', '?'))} · {esc(m.get('quality', ''))}"
        if m.get("score") is not None:
            line += f" · match {m['score']:.0f}"
        if (m.get("copies") or 0) > 1:
            line += f" · {m['copies']} copies"
        out.append(line)
        extra = f" · {fmt_speed(self.speed)}" if self.speed else ""
        out.append(f"{self.pct:.0f}%{extra}")
        return "\n".join(out)[:MESSAGE_LIMIT]


# --- final summaries -----------------------------------------------------------

def _sources_line(source_counts: dict[str, int] | None) -> str | None:
    if not source_counts:
        return None
    ranked = sorted(source_counts.items(), key=lambda kv: -kv[1])
    parts = [f"{esc(peer)} ×{n}" if n > 1 else esc(peer) for peer, n in ranked[:3]]
    line = "from " + ", ".join(parts)
    if len(ranked) > 3:
        line += f" +{len(ranked) - 3} more peers"
    return line


def _failure_block(failed: list[tuple[str, str]], budget: int) -> list[str]:
    """Bullet list of failed tracks with human reasons. Groups by reason when
    the list is long; trims to fit the character budget."""
    lines: list[str] = [f"❌ {len(failed)} not downloaded:"]
    if len(failed) <= 5:
        for title, reason in failed:
            lines.append(f"· {esc(_trunc(title, 40))} — {esc(humanize_failure(reason))}")
    else:
        by_reason: dict[str, list[str]] = {}
        for title, reason in failed:
            by_reason.setdefault(humanize_failure(reason), []).append(title)
        for reason, titles in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            shown = ", ".join(esc(_trunc(t, 28)) for t in titles[:3])
            more = f" +{len(titles) - 3} more" if len(titles) > 3 else ""
            lines.append(f"· {esc(reason)}: {shown}{more}")

    while len("\n".join(lines)) > budget and len(lines) > 2:
        lines.pop()
        hidden = len(failed) - (len(lines) - 1)
        if hidden > 0:
            lines[-1] += f"\n  +{hidden} more"
            break
    return lines


def render_album_final(
    artist: str,
    title: str,
    result: dict,
    scan_note: str = "",
    share_url: str | None = None,
) -> str:
    """Final album summary. Kept within the photo-caption limit (1024) since
    it usually rides on the cover-art message."""
    downloaded = result.get("downloaded", 0)
    failed = result.get("failed") or []
    skipped = result.get("skipped", 0)
    wanted = downloaded + len(failed)

    if failed and downloaded:
        icon = "⚠️"
    elif failed:
        icon = "❌"
    else:
        icon = "✅"
    out = [f"{icon} <b>{esc(artist)} — {esc(title)}</b>"]

    stats = []
    if wanted:
        stats.append(f"{downloaded}/{wanted} saved")
    if skipped:
        stats.append(f"{skipped} already in library")
    if result.get("format"):
        stats.append(esc(result["format"]))
    if result.get("with_lyrics") and downloaded:
        stats.append(f"lyrics {result['with_lyrics']}/{downloaded}")
    if result.get("elapsed_secs"):
        stats.append(fmt_duration(result["elapsed_secs"]))
    if stats:
        out.append(" · ".join(stats))

    src = _sources_line(result.get("source_counts"))
    if src:
        out.append(src)

    if failed:
        out.append("")
        used = len("\n".join(out)) + len(scan_note) + len(share_url or "") + 8
        out += _failure_block(failed, budget=CAPTION_LIMIT - used)

    if scan_note:
        out.append("")
        out.append(esc(scan_note))
    if share_url:
        out.append("")
        out.append(share_url)
    return "\n".join(out)[:CAPTION_LIMIT]


def render_track_final(
    artist: str,
    title: str,
    fmt: str | None,
    peer: str | None,
    elapsed_secs: float | None,
    scan_note: str = "",
    share_url: str | None = None,
) -> str:
    out = [f"✅ <b>{esc(artist)} — {esc(title)}</b>"]
    stats = []
    if fmt:
        stats.append(esc(fmt))
    if peer:
        stats.append(f"from {esc(peer)}")
    if elapsed_secs:
        stats.append(fmt_duration(elapsed_secs))
    if stats:
        out.append(" · ".join(stats))
    if scan_note:
        out.append("")
        out.append(esc(scan_note))
    if share_url:
        out.append("")
        out.append(share_url)
    return "\n".join(out)[:CAPTION_LIMIT]
