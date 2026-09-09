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
`install -d -o UID -g GID -m 0775` using your numeric IDs. The library root has
to be writable and searchable by that user or group.

## Migrating an installation that ran as root

Stop both services first. Record existing numeric ownership and modes in a
private rollback manifest before changing anything; where ACL tools are
available, `getfacl -R -p` and `setfacl --restore=MANIFEST` capture and restore
this metadata. Keep the manifest outside the repository — it contains your paths.

Then, with your own numeric IDs:

1. `chown -R UID:GID bot-data slskd-config` — **including `bot-data/uploads` and
   everything under it**. Those subdirectories were created by the root process
   and are the one place where a missed `chown` is silent: `os.makedirs` on an
   existing directory succeeds, and the upload watcher only fails later, when it
   tries to write into `.incoming` or `.extracted`.
2. `chown -R UID:GID` and `chmod -R g+w` on `$MUSIC_LIBRARY_DIR/.slskd-downloads`,
   the staging area — partially downloaded files there were created by root and
   have no group write, so the non-root slskd cannot finish them.
3. In the library itself, preserve existing owners and give the chosen group
   read/write on files and read/write/search on directories:

       find $MUSIC_LIBRARY_DIR -not -path '*/lost+found*' -type f ! -perm -g+w -exec chmod g+w {} +
       find $MUSIC_LIBRARY_DIR -not -path '*/lost+found*' -type d ! -perm -g+w -exec chmod g+w {} +

   Tagging edits the file in place, so a track without group write cannot be
   retagged even though it can be read, and importing another disc into an
   existing album does not go through directory creation — nothing will repair
   such a mode later.
4. Optional but worth doing: set the setgid bit on the library directories
   (`find $MUSIC_LIBRARY_DIR -type d -not -name 'lost+found' -exec chmod g+s {} +`).
   New files then inherit the directory's group whoever creates them, which keeps
   the invariant even for a writer whose primary group is something else — a
   Samba client, say. It is reversible with `chmod g-s`.

Leave unrelated directories such as `lost+found` alone.

Record the running image and Compose configuration for rollback. Recreating a
container preserves bind-mounted state, music and the download journal, but loses
any unbuilt `docker cp` edits: reconcile those with the image first.

Recreate both services, check that the processes are non-root and healthy, then
exercise upload staging, journal persistence, import into an existing album, and
tagging a file owned by another library writer. On failure, stop both services,
restore the permission manifest and the previous image and configuration, restart
and check health. Do not delete the data volumes.
