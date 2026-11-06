"""Read-only field previews and atomic application of the exact selected values."""

import asyncio
from dataclasses import dataclass, field
import errno
import os
import shutil
import stat

import aiohttp

import metadata
from metadata.client import _get_session
import retagger
from library.files import _cover_url
from library.importing import run_to_completion
from library.writing import library_temporary
from library.tagger import inspect_selected_tags, write_selected_tags, _format_title

FIELDS = ("titles", "genres", "lyrics", "artwork")


@dataclass(frozen=True)
class Options:
    fields: frozenset[str]
    replace_lyrics: bool = False
    replace_artwork: bool = False

    def __post_init__(self):
        if not self.fields or not self.fields.issubset(FIELDS):
            raise ValueError("Select at least one supported field")

    def describe(self) -> str:
        labels = []
        for name in FIELDS:
            if name not in self.fields:
                continue
            if name in {"lyrics", "artwork"}:
                replace = self.replace_lyrics if name == "lyrics" else self.replace_artwork
                labels.append(f"{name} ({'replace existing' if replace else 'fill missing'})")
            else:
                labels.append(name)
        return ", ".join(labels)


@dataclass
class FileUpdate:
    path: str
    fingerprint: tuple | None
    values: dict


@dataclass
class Plan(retagger.AlbumPlan):
    updates: list[FileUpdate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    write_errors: list[tuple[str, str]] = field(default_factory=list)


def fingerprint(path: str) -> tuple:
    stat = os.stat(path, follow_symlinks=False)
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _safe_file(path: str, folder: str) -> bool:
    return (os.path.realpath(path).startswith(os.path.realpath(folder) + os.sep)
            and not os.path.islink(folder) and not os.path.islink(os.path.dirname(folder))
            and not os.path.islink(path) and os.path.isfile(path))


def _target_unchanged(update: FileUpdate, folder: str) -> bool:
    if update.fingerprint is None:
        return (os.path.dirname(update.path) == folder and not os.path.islink(folder)
                and not os.path.islink(os.path.dirname(folder)) and not os.path.lexists(update.path))
    return _safe_file(update.path, folder) and fingerprint(update.path) == update.fingerprint


async def fetch_artwork(album: dict) -> bytes | None:
    url = _cover_url(album.get("cover_uuid", ""))
    if not url:
        return None
    session = await _get_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > 8 * 1024**2:
                    return None
            return bytes(data) if data.startswith(b"\xff\xd8\xff") else None
    except (aiohttp.ClientError, TimeoutError):
        return None


async def plan_album(folder: str, artist: str, album: str, options: Options) -> Plan:
    base = await retagger.plan_album(folder, artist, album, include_genres="genres" in options.fields)
    plan = Plan(folder=folder, artist_dir=artist, album_dir=album, files=base.files,
                album_id=base.album_id, fresh_meta=base.fresh_meta,
                lastfm_only_genres=base.lastfm_only_genres, error=base.error)
    if plan.error:
        return plan
    fresh = plan.fresh_meta or {}
    genres = fresh.get("genres") or plan.lastfm_only_genres
    cover = None
    cover_fetched = False
    had_embedded_art = False
    for path in plan.files:
        try:
            if not _safe_file(path, folder):
                raise ValueError("File leaves album directory")
            before = fingerprint(path)
            existing = await asyncio.to_thread(inspect_selected_tags, path)
            had_embedded_art = had_embedded_art or bool(existing["artwork"])
            summary = await asyncio.to_thread(retagger._read_file_summary, path)
            track = retagger._match_track(summary, fresh.get("tracks") or [])
            values, changes = {}, []
            label = os.path.relpath(path, folder)
            if "genres" in options.fields and genres:
                merged = list(existing["genres"])
                known = {g.casefold() for g in merged}
                added = []
                for genre in genres:
                    if genre.casefold() not in known:
                        merged.append(genre)
                        added.append(genre)
                        known.add(genre.casefold())
                if added:
                    values["genres"] = merged
                    changes.append("genres: +" + ", ".join(added))
            if track:
                if "titles" in options.fields and existing["title"] != _format_title(track):
                    values["title"] = _format_title(track)
                    changes.append(f"title: {existing['title']!r} → {values['title']!r}")
                if "lyrics" in options.fields and (options.replace_lyrics or not existing["has_synced"]):
                    try:
                        lyrics = await metadata.fetch_lyrics(track["title"], track.get("artist") or artist,
                                                             fresh.get("title") or album, track.get("duration", 0))
                    except Exception:
                        lyrics = None
                    if lyrics and lyrics.get("syncedLyrics"):
                        plain = (lyrics.get("plainLyrics") or "") if options.replace_lyrics else (
                            existing["plain"] or lyrics.get("plainLyrics") or "")
                        if lyrics["syncedLyrics"] != existing["synced"] or plain != existing["plain"]:
                            values["lyrics"] = {"syncedLyrics": lyrics["syncedLyrics"], "plainLyrics": plain,
                                                "replaceExisting": options.replace_lyrics}
                            changes.append("lyrics: replace existing" if existing["has_synced"] or
                                           options.replace_lyrics and existing["plain"] else "lyrics: add synced")
                    else:
                        plan.notes.append(f"{label}: synced lyrics unavailable")
                if "artwork" in options.fields and (options.replace_artwork or not existing["artwork"]):
                    if not cover_fetched:
                        cover = await fetch_artwork(fresh)
                        cover_fetched = True
                    if cover and cover not in existing["artwork"]:
                        values["artwork"] = cover
                        changes.append("artwork: replace existing" if existing["artwork"] else "artwork: add")
                    elif not cover:
                        plan.notes.append(f"{label}: artwork unavailable")
            elif options.fields - {"genres"}:
                plan.notes.append(f"{label}: no matching track; selected track fields skipped")
            if fingerprint(path) != before:
                raise ValueError("File changed during preview")
            if values:
                plan.updates.append(FileUpdate(path, before, values))
                plan.changes.extend(f"{label} · {change}" for change in changes)
        except (OSError, ValueError) as exc:
            plan.notes.append(f"{os.path.basename(path)}: {exc}")
    if "artwork" in options.fields and plan.fresh_meta:
        # The import pipeline writes cover.jpg; update that visible album cover too.
        # Fill mode preserves existing cover/folder image sidecars.
        cover_path = os.path.join(folder, "cover.jpg")
        existing_cover = any(name.casefold().startswith(("cover.", "folder."))
                             for name in os.listdir(folder))
        if options.replace_artwork or not existing_cover and not had_embedded_art:
            before = fingerprint(cover_path) if os.path.lexists(cover_path) else None
            if not cover_fetched:
                cover = await fetch_artwork(fresh)
            if cover and not os.path.islink(cover_path):
                current = None
                if os.path.isfile(cover_path):
                    with open(cover_path, "rb") as stream:
                        current = stream.read(8 * 1024**2 + 1)
                if current != cover:
                    plan.updates.append(FileUpdate(cover_path, before, {"cover_file": cover}))
                    plan.changes.append("cover.jpg · artwork: replace existing" if before else "cover.jpg · artwork: add")
            elif not cover and not any("artwork unavailable" in note for note in plan.notes):
                plan.notes.append("cover.jpg: artwork unavailable")
    return plan


async def preview(albums: list[tuple[str, str, str]], options: Options, progress=None):
    plans = []
    for folder, artist, album in albums:
        try:
            plan = await plan_album(folder, artist, album, options)
        except Exception as exc:
            plan = Plan(folder=folder, artist_dir=artist, album_dir=album, error=str(exc))
        plans.append(plan)
        if progress:
            await progress(len(plans), len(albums), plan)
    summary = retagger.RetagSummary(total=len(plans),
                                   unidentified=sum(bool(p.error) for p in plans),
                                   will_change=sum(p.needs_apply for p in plans),
                                   no_changes=sum(not p.error and not p.needs_apply for p in plans))
    return plans, summary


def _check_replaceable(update: FileUpdate):
    """Require real file write access even though rename only needs the folder.

    Atomic replacement cannot retain a foreign owner's inode, hard links or
    access ACL. Shared ordinary files use directory inheritance; refuse files
    whose link/access policy would be lost. Never chown/chmod the original.
    """
    if update.fingerprint is None:
        return
    descriptor = os.open(update.path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if not stat.S_ISREG(info.st_mode) or current != update.fingerprint:
            raise ValueError("File changed since preview")
        if not info.st_mode & 0o222:
            raise PermissionError("Read-only file cannot be replaced")
        if info.st_nlink != 1:
            raise ValueError("Atomic maintenance cannot preserve hard links")
        try:
            acl = os.getxattr(descriptor, "system.posix_acl_access")
        except OSError as exc:
            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
        else:
            if acl:
                raise ValueError("Atomic maintenance cannot preserve file access ACLs")
    finally:
        os.close(descriptor)


def _write_atomic(update: FileUpdate, folder: str):
    if not _target_unchanged(update, folder):
        raise ValueError("File changed since preview")
    _check_replaceable(update)
    with library_temporary(update.path) as (temporary, stream):
        if "cover_file" in update.values:
            stream.write(update.values["cover_file"])
            stream.close()
        else:
            with open(update.path, "rb") as source:
                shutil.copyfileobj(source, stream)
            stream.close()
            write_selected_tags(temporary, update.values)
        if not _target_unchanged(update, folder):
            raise ValueError("File changed while applying update")
        _check_replaceable(update)
        if update.fingerprint is None:
            # Another writer may have created artwork after our final check.
            os.link(temporary, update.path)
        else:
            os.replace(temporary, update.path)


async def apply_plan(plan: Plan) -> tuple[int, int]:
    written = 0
    for update in plan.updates:
        try:
            await run_to_completion(asyncio.to_thread(_write_atomic, update, plan.folder))
            written += 1
        except Exception as exc:
            plan.write_errors.append((os.path.basename(update.path), str(exc)))
    sidecars = sum("cover_file" in update.values for update in plan.updates)
    return written, len(plan.files) + sidecars - written
