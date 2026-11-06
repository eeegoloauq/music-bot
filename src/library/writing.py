"""Content-only library writes under the process umask and directory policy.

See docs/volume-permissions.md before changing creation or replacement semantics.
"""

from contextlib import contextmanager, suppress
import os
import secrets
import shutil


@contextmanager
def library_temporary(destination: str):
    """Yield a fresh (path, binary stream) beside destination; always clean up.

    Unlike mkstemp's 0600, 0666 respects the library's existing umask/default
    ACL. O_EXCL protects the random name from collisions and symlink following.
    A scanner must never treat an interrupted copy as a finished audio file.
    """
    folder = os.path.dirname(destination)
    while True:
        temporary = os.path.join(folder, f".musicbot-{secrets.token_hex(16)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(descriptor, "wb") as stream:
            yield temporary, stream
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


@contextmanager
def new_library_file(destination: str, *, prepare=None):
    """Publish complete content without overwriting any existing destination.

    Linking within the destination directory atomically fails if another writer
    claimed the name. Removing the temporary link leaves one ordinary file.
    Optional preparation edits the closed temporary file before publication.
    """
    with library_temporary(destination) as (temporary, stream):
        yield stream
        stream.close()
        if prepare is not None:
            prepare(temporary)
        os.link(temporary, destination)


def copy_into_library(source: str, destination: str, *, prepare=None):
    """Copy content without source metadata, optionally prepare, then publish.

    Keep the source and never replace an existing library file.
    """
    with open(source, "rb") as src, new_library_file(destination, prepare=prepare) as dst:
        shutil.copyfileobj(src, dst)
