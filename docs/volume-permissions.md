# Container users and volume permissions

Music Bot runs as a non-root user. The optional local slskd container uses the same user. Configure `MUSICBOT_UID` and
`MUSICBOT_GID` in `.env`; both default to 1000. `MUSICBOT_UID` owns the bot's
own state, and **`MUSICBOT_GID` must be the group that owns the music library**,
not the user's private group — that single choice is what makes every write land
in the right group without anything chmod-ing afterwards.

Group write comes from the umask, not from a later chmod. The bot sets
`umask 0002` at start-up and slskd gets the same through `SLSKD_UMASK`, so new
ordinary files request mode `0666` and become `0664`; directories request `0777`
and become `0775`. Setgid directories also retain/inherit the setgid bit, usually
making their mode `2775`, and make new files and subdirectories inherit the shared
library group. The bot must belong to that group. Directory default ACLs, where
configured, also participate in creation; the simple mode examples assume none.

Runtime workflows must not repair existing library permissions with chmod/chown,
recursively or per file. A non-root process cannot chmod a file another writer
owns even when the group grants write access. Do not change the process umask
temporarily in async tasks or worker threads: it affects every concurrent write.
Set it once at startup. Operator-controlled migration remains a separate action.

## Content copies and temporary files

Local original and matched imports always create a new destination inode through
`library/writing.py`, including when intake and library share a filesystem.
`copyfileobj()` copies content only. A source arriving `0644`, `0600` or with
execute bits does not impose those modes on the new library file. The importing
user must still be able to read the source and remove it from staging.
Matched imports apply release tags to the new temporary copy before publication;
they do not require write access to the supplied source inode.

**Keep original files** preserves every content byte, including tags, embedded
artwork and lyrics. It also retains filenames and supported sidecar contents.
It does not promise original ownership, modes, timestamps, ACLs or other filesystem
metadata. Those are governed by destination creation policy. Imports reserve a
separate album directory and publish completed files without overwriting an
existing name. Retried sidecars are accepted only if their complete bytes match;
a different existing file causes failure and remains intact.

`shutil.copy2()` also copies filesystem metadata through `copystat()`, including
source permission bits and, on Linux where available, extended attributes.
`copy()`, `copymode()` and `copystat()` likewise copy modes and do not belong in
these new-library-file paths. Applying `st_mode & 0o777` extracts the source's
nine ordinary permission bits. It does **not** mean setting mode `0777`; the
policy error is propagating source permissions instead of using library creation
policy. Copying bits also says nothing about retaining ownership or an inode.

Maintenance and imports use unpredictable names ending in `.tmp`, created in
the destination directory with `O_CREAT | O_EXCL` and mode `0666`. The existing
umask and directory inheritance apply immediately. Exclusive creation prevents
following a pre-existing symlink at that temporary name. The suffix keeps partial
audio out of library scanners. Streams are closed before publication, temporary
paths are removed on ordinary failure, and cancellation waits for the worker to
finish before returning. A process crash can leave an ignored `.tmp` file; there
is no recursive runtime cleanup or permission repair.

Removing a chmod after `mkstemp()` would be insufficient: `mkstemp()` requests
`0600`, so the replacement would still exclude other group writers. A deliberately
justified mode change on a newly created, bot-owned temporary inode would differ
from repairing somebody else's library file, but these workflows avoid it by
requesting the correct creation mode in the first place. They never request audio
execute bits or world write under the configured umask.

New imports and artwork use a same-directory hard link to publish the complete
temporary inode with an exclusive destination name, then remove the temporary
link. This prevents a race from overwriting an existing library file. Filesystems
must support hard links; unsupported publication fails with the source retained.
Downloaded artwork uses the same helper and retains a cover supplied by another
writer while the network request was in progress.

## Maintenance replacement semantics

Selected maintenance checks preview fingerprints before preparation and again
before publication. It prepares bytes and tag changes in a temporary inode;
preparation failure leaves the original content and inode intact. New artwork
uses exclusive publication. Existing audio and artwork use `os.replace()` for
atomic visibility at the pathname. This is a per-file guarantee, not an album
transaction or a promise of durability across power loss.

`os.replace()` installs a **new inode**. It does not preserve the original owner's
UID, per-file ACLs, other extended attributes or hard-link relationships merely
because mode bits happen to match. The replacement belongs to the bot UID and
inherits its group and creation policy from the album directory. Other processes
with the old inode open can continue seeing the old content. There is no chown
attempt and no requirement to run as root.

Retaining a foreign owner's inode would require editing it in place, which
conflicts with keeping the original intact after a failed tag write. Selected
maintenance chooses atomic replacement for ordinary files in the shared library.
Before preparation and just before replacement it opens the existing file for
writing **without truncation**, checking the opened inode against the preview.
Directory write access alone is insufficient: a foreign-owned `0644` file or
another non-writable target is refused rather than bypassed through rename.

Maintenance also refuses files with multiple hard links or Linux POSIX access
ACLs, reporting a per-file error and retaining the original. This includes access
ACLs inherited from a directory default ACL: it does not try to infer whether
discarding an ACL would be safe. Default-ACL creation still works for imports and
new artwork, but subsequent maintenance of those access-ACL files is refused.
The ACL guard targets Linux POSIX ACLs on the container's filesystem; NFSv4/SMB
server-specific ACL behavior is not verified by the local filesystem tests.

Bot writes serialize internally. Fingerprints detect external changes made
during preparation; a separate writer is not locked out between the final check
and rename. Coordinate external writers for this short publication window. These
checks are not a filesystem compare-and-swap or protection against a hostile
process with write access to the same directory.

## Download staging

A rename keeps the mode the file was created with, which is why the download
side needs the same umask as the bot: a file that arrives `0644` in staging is
still `0644` after it is renamed into the library. `SLSKD_UMASK` is read by
slskd's Docker entrypoint, so it only applies when slskd runs from that image.
When the download move crosses a filesystem, its content-only copy gets the
bot's creation policy. Local uploads use the content-only import path described
above regardless of filesystem boundaries. Intake may rename supplied folders
or loose files and retain their modes while staging; staging is not publication
into the shared library. ZIP extraction and browser intake create files normally
without applying archive/source modes. Bot state temporary files are outside the
library and follow their state directory's access policy.

Do not combine slskd's `user:` with its `PUID`/`PGID` variables; it refuses to
start when both are set.

## A fresh install

Create `bot-data` with `install -d -o UID -g GID -m 2775` using your numeric IDs.
The optional local Soulseek provider also needs `slskd-config` and
`$MUSIC_LIBRARY_DIR/.slskd-downloads` created with those permissions. The setgid bit is
what keeps whatever lands there in the shared group. The library root has
to be writable and searchable by that user or group.

## Migrating an installation that ran as root

Stop both services, then from the directory that holds `compose.yaml`:

    sudo ./scripts/migrate-permissions.sh

It reads `.env`, takes the library group from the library directory itself,
prints what it is about to change and asks first. `--yes` skips the question.
Running it twice is harmless.

It hands over three directories, and only these: `bot-data`, `slskd-config` and
`$MUSIC_LIBRARY_DIR/.slskd-downloads`, each with everything under it.
`bot-data/uploads` is where a missed `chown` stays silent — `os.makedirs` on an
existing directory succeeds, and the upload watcher only fails later, writing
into `.incoming` or `.extracted`.

It refuses to run when `MUSIC_LIBRARY_DIR` names a system directory or sits one
level below the root, when the library group resolves to root, when the IDs are
not numeric, or when one of the three targets is a symlink. The path is checked
as written and again after symlinks are resolved, because `/bin` is `/usr/bin`
on a merged-usr system and a check that saw only one spelling would let the
other through.

**It does not touch the library.** A recursive `chgrp` as root over a path taken
from a config file has no safe failure mode, so the script prints the commands
the library needs with your values already substituted, and you run them having
read them. They look like this:

    sudo find LIBRARY -xdev -name lost+found -prune -o ! -type l ! -group GID -exec chgrp GID -- {} +
    sudo find LIBRARY -xdev -name lost+found -prune -o ! -type l ! -perm -g+w -exec chmod g+rwX -- {} +
    sudo find LIBRARY -xdev -name lost+found -prune -o -type d ! -perm -g+s -exec chmod g+s -- {} +

The group has to change, not just the mode. Group write on a track still owned
by `root:root` buys nothing. In-place retagging needs file write access, and
selected atomic maintenance deliberately requires it too, so a track without it
cannot be retagged even though it can be read. Importing another disc
into an existing album does not go through directory creation either, so nothing
repairs such a mode later. The setgid bit on the third line is what keeps a new
file in the library's group whoever writes it.

Owners are kept, `lost+found` is skipped, and `-xdev` keeps a bind mount inside
the library out of it.

Want a rollback manifest first? Where ACL tools are available, `getfacl -R -p`
records ownership and modes and `setfacl --restore=MANIFEST` puts them back.
Keep it outside the repository, it contains your paths.

Should the volumes still be wrong when the bot starts, it says so and exits
instead of running with nothing it can write to.

Record the running image and Compose configuration for rollback. Recreating a
container preserves bind-mounted state, music and the download journal, but loses
any unbuilt `docker cp` edits: reconcile those with the image first.

Recreate both services, check that the processes are non-root and healthy, then
exercise upload staging, journal persistence, import into an existing album, and
tagging a file owned by another library writer. On failure, stop both services,
restore the permission manifest and the previous image and configuration, restart
and check health. Do not delete the data volumes.
