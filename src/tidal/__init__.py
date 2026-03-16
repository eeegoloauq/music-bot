from tidal.client import close, resolve_link
from tidal.metadata import fetch_album, fetch_single_track, search, fetch_cover_url, fetch_lyrics
from tidal.downloader import download_album, download_single_track, ProgressCallback

__all__ = [
    "close", "resolve_link",
    "fetch_album", "fetch_single_track", "search", "fetch_cover_url", "fetch_lyrics",
    "download_album", "download_single_track", "ProgressCallback",
]
