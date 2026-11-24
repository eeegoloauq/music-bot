"""Fill a missing FLAC STREAMINFO sample count without changing the audio."""

import logging
import re
import subprocess
import tempfile

from mutagen.flac import FLAC

logger = logging.getLogger(__name__)
_FRAME_BLOCK_SIZE = re.compile(rb"\tblocksize=([1-9][0-9]*)\t")
_MAX_SAMPLES = (1 << 36) - 1


def restore_sample_count(path: str) -> int:
    """Set a zero count from verified audio frames. Return the number added."""
    audio = FLAC(path)
    if audio.info.total_samples:
        return 0

    # The reference decoder validates the FLAC stream and reports one block size
    # per frame, including the shorter final block. Stream the report so neither
    # the decoded PCM nor the complete report has to be kept in memory.
    with tempfile.TemporaryFile() as errors:
        try:
            decoder = subprocess.Popen(
                ["flac", "--analyze", "--stdout", "--silent", "--", path],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Install the flac command to read the missing sample count") from exc

        with decoder:
            samples = 0
            for line in decoder.stdout:
                if not line.startswith(b"frame="):
                    continue
                block = _FRAME_BLOCK_SIZE.search(line)
                if block is None:
                    decoder.kill()
                    raise ValueError("FLAC decoder did not report a frame's sample count")
                samples += int(block[1])
                if samples > _MAX_SAMPLES:
                    decoder.kill()
                    raise ValueError("FLAC sample count exceeds STREAMINFO capacity")
            if decoder.wait() != 0:
                errors.seek(0)
                logger.warning("FLAC analysis failed for %s: %s", path,
                               errors.read(4096).decode(errors="replace").strip())
                raise ValueError("Could not count samples in the FLAC audio stream")
    if not samples:
        raise ValueError("No audio frames found in the FLAC file")

    audio.info.total_samples = samples
    audio.save(padding=lambda info: info.padding)
    return samples
