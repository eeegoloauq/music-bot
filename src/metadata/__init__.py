from metadata.api import (
    fetch_album,
    fetch_single_track,
    search,
    fetch_cover_url,
    fetch_lyrics,
)
from metadata.resolver import resolve_link
from metadata.client import close

__all__ = [
    "close",
    "resolve_link",
    "fetch_album",
    "fetch_single_track",
    "search",
    "fetch_cover_url",
    "fetch_lyrics",
]
