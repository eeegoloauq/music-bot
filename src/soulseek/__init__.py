from soulseek.client import close, rescan_shares, schedule_rescan_shares
from soulseek.downloader import (
    download_album,
    download_single_track,
    find_lossy_candidates,
    ProgressCallback,
)

__all__ = [
    "close",
    "rescan_shares",
    "schedule_rescan_shares",
    "download_album",
    "download_single_track",
    "find_lossy_candidates",
    "ProgressCallback",
]
