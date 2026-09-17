#!/usr/bin/env python3
"""Repair TITLE tags that came out as "Song (Remix) ((Remix))" (music-bot < 3.0.2).

Runs inside the bot image, which already has mutagen and the library mounted:

    docker compose run --rm --no-deps \
        -v "$PWD/scripts:/scripts:ro" music-bot python /scripts/fix-doubled-title-version.py

Prints what it would change. Add --apply to write.
"""
import os
import re
import sys

from mutagen import File

DOUBLED = re.compile(r"^(.*) \((.+?)\) \(\(\2\)\)$")
EXTS = (".flac", ".m4a", ".mp3")


def main() -> None:
    apply = "--apply" in sys.argv
    root = next((a for a in sys.argv[1:] if not a.startswith("--")), "/music")
    count = 0
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if not name.lower().endswith(EXTS) or "(" not in name:
                continue
            path = os.path.join(dirpath, name)
            audio = File(path, easy=True)
            if audio is None:
                continue
            match = DOUBLED.match((audio.get("title") or [""])[0])
            if not match:
                continue
            fixed = f"{match.group(1)} ({match.group(2)})"
            print(f"{path}: {fixed!r}")
            if apply:
                audio["title"] = fixed
                audio.save()
            count += 1
    print(f"{'fixed' if apply else 'would fix'} {count}")


if __name__ == "__main__":
    main()
