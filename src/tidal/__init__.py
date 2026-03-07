from tidal.client import close, resolve_link
from tidal.metadata import fetch_album, fetch_single_track, search, fetch_cover_url
from tidal.downloader import download_album, download_single_track, ProgressCallback

__all__ = [
    "close", "resolve_link",
    "fetch_album", "fetch_single_track", "search", "fetch_cover_url",
    "download_album", "download_single_track", "ProgressCallback",
]
