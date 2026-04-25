from soulseek.client import close
from soulseek.downloader import (
    download_album,
    download_single_track,
    find_lossy_candidates,
    ProgressCallback,
)

__all__ = [
    "close",
    "download_album",
    "download_single_track",
    "find_lossy_candidates",
    "ProgressCallback",
]
