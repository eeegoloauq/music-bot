# Container users and volume permissions

Both containers run as the same non-root user. Configure `MUSICBOT_UID` and
`MUSICBOT_GID` in `.env`; both default to 1000. `MUSICBOT_UID` owns the bot's
own state, and **`MUSICBOT_GID` must be the group that owns the music library**,
not the user's private group — that single choice is what makes every write land
in the right group without anything chmod-ing afterwards.

Group write comes from the umask, not from a later chmod. The bot sets
`umask 0002` at start-up and slskd gets the same through `SLSKD_UMASK`, so new
files are created `0664` and new directories `0775` and are immediately usable by
every other writer in the group. Nothing in the code chmods a path: a non-root
process cannot change the mode of a file another writer owns, so a chmod after
the fact could only ever patch up a mode that was already wrong.

A rename keeps the mode the file was created with, which is why the download
side needs the same umask as the bot: a file that arrives `0644` in staging is
still `0644` after it is renamed into the library. `SLSKD_UMASK` is read by
slskd's Docker entrypoint, so it only applies when slskd runs from that image.
When the move crosses a filesystem — the upload intake under `/data` into the
library — the copy is written through `open()` on purpose, so the new file gets
this process's umask instead of the mode of whoever dropped the source. That
matters for files handed over by a Samba or SFTP client, which commonly arrive
`0644`.

Do not combine slskd's `user:` with its `PUID`/`PGID` variables; it refuses to
start when both are set.

## A fresh install

Create `bot-data`, `slskd-config` and `$MUSIC_LIBRARY_DIR/.slskd-downloads` with
`install -d -o UID -g GID -m 2775` using your numeric IDs. The setgid bit is
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
by `root:root` buys nothing, and tagging edits the file in place, so a track
without it cannot be retagged even though it can be read. Importing another disc
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
