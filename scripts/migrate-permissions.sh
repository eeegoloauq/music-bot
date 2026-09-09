#!/usr/bin/env bash
# Hand this stack's own directories over to the non-root user the containers
# now run as, and print what is left to do in the music library.
#
#     docker compose down
#     sudo ./scripts/migrate-permissions.sh
#     docker compose up -d
#
# Everything is read from .env, so there is nothing to fill in.
#
# The library itself is deliberately not touched here. A recursive chgrp as root
# over a path taken from a config file has no safe failure mode: one stale value
# and it walks somewhere it should never have been. What the library needs is
# printed instead, with your values already substituted, so you run it having
# read it.
set -euo pipefail

yes=0
for arg in "$@"; do
  case "$arg" in
    --yes) yes=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "$*" >&2; exit 1; }

[ -f compose.yaml ] || die "Run this from the directory that holds compose.yaml."
[ -f .env ] || die "No .env here. Copy .env.example first."

# Tolerates surrounding quotes, spaces around =, an "export " prefix, a trailing
# CR from a Windows editor and a trailing comment. Last assignment wins, the way
# compose reads it.
get() {
  sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*//p" .env \
    | sed -E 's/[[:space:]]+#.*$//; s/\r$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' \
    | tail -1
}

# Refuse anything that is a system directory rather than a media library.
# Checked on the value as written and again after symlinks are resolved: on a
# merged-usr system /bin resolves to /usr/bin, and an exact-match list that only
# saw one of the two spellings would wave it through.
reject_system_path() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/tmp)
      die "MUSIC_LIBRARY_DIR looks wrong: $1" ;;
    /bin/*|/boot/*|/dev/*|/etc/*|/lib/*|/lib64/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/usr/*|/var/*)
      die "MUSIC_LIBRARY_DIR looks wrong: $1" ;;
  esac
  # /a is never a library; /a/b may well be.
  case "$1" in
    /*/*) ;;
    *) die "MUSIC_LIBRARY_DIR is too close to the root: $1" ;;
  esac
}

raw_library=$(get MUSIC_LIBRARY_DIR); raw_library=${raw_library:-/media/music}
uid=$(get MUSICBOT_UID); uid=${uid:-1000}
gid=$(get MUSICBOT_GID)

reject_system_path "${raw_library%/}"
library=$(readlink -f -- "$raw_library" 2>/dev/null || true)
[ -n "$library" ] && [ -d "$library" ] || die "MUSIC_LIBRARY_DIR is not a directory: $raw_library"
reject_system_path "$library"

case "$uid$gid" in *[!0-9]*) die "MUSICBOT_UID and MUSICBOT_GID must be numeric." ;; esac

library_gid=$(stat -c %g -- "$library")
if [ -z "$gid" ]; then
  gid=$library_gid
  write_env=1
else
  write_env=0
fi
[ "$gid" -ne 0 ] || die "The library group is root. Set MUSICBOT_GID in .env to the group that owns your music, then run this again."
if [ "$gid" -ne "$library_gid" ]; then
  echo "Note: MUSICBOT_GID is $gid but $library belongs to group $library_gid."
  echo
fi

staging="$library/.slskd-downloads"
targets=(bot-data slskd-config "$staging")

# A symlink here would send the whole recursion wherever it points.
check_targets() {
  for d in "${targets[@]}"; do
    [ ! -L "$d" ] || die "$d is a symlink. Point it at a real directory, or set its permissions yourself."
  done
}
check_targets

echo "Library      $library"
echo "Runs as      $uid:$gid"
echo
echo "Handing over:"
for d in "${targets[@]}"; do
  if [ -e "$d" ]; then
    echo "  $d — $(find "$d" -xdev ! -type l \( ! -user "$uid" -o ! -group "$gid" \) | wc -l) path(s)"
  else
    echo "  $d — missing, will be created"
  fi
done
[ "$write_env" = 1 ] && { echo; echo "MUSICBOT_UID=$uid and MUSICBOT_GID=$gid will be written to .env."; }

if [ "$yes" = 0 ]; then
  printf '\nProceed? [y/N] '
  read -r answer
  case "$answer" in [yY]*) ;; *) echo "Nothing changed."; exit 1 ;; esac
fi

# Re-checked after the question: the answer took time, and the check before it
# is only as fresh as the moment it ran.
check_targets

for d in "${targets[@]}"; do
  [ -e "$d" ] || install -d -o "$uid" -g "$gid" -m 2775 -- "$d"
  # find rather than chown -R / chmod -R: it does not descend a symlinked
  # argument and ! -type l keeps it off symlinks inside the tree. chmod -R
  # follows the argument, and on BusyBox chown -R follows links as well.
  find "$d" -xdev ! -type l -exec chown "$uid:$gid" -- {} +
  find "$d" -xdev ! -type l -exec chmod g+rwX -- {} +
done

if [ "$write_env" = 1 ]; then
  printf '\n# Numeric host IDs the containers run as.\nMUSICBOT_UID=%s\nMUSICBOT_GID=%s\n' "$uid" "$gid" >> .env
fi

# What is left is the library, and it is yours to change.
# A function, not a string fed to eval: the path comes from a config file and
# has no business being re-parsed by the shell.
walk() { find "$library" -xdev -name lost+found -prune -o ! -type l "$@"; }
wrong_group=$(walk ! -group "$gid" -print | wc -l)
no_write=$(walk ! -perm -g+w -print | wc -l)
no_sgid=$(walk -type d ! -perm -g+s -print | wc -l)

echo
if [ "$((wrong_group + no_write + no_sgid))" -eq 0 ]; then
  echo "The library is already in group $gid and group-writable. Nothing left to do."
else
  cat <<EOF
The library still needs three things. Group write alone is not enough: a track
owned by root:root cannot be written by $uid:$gid, so the group has to change
too. File owners are kept, lost+found and other filesystems are skipped.

  $wrong_group path(s) not in group $gid
  $no_write path(s) without group write
  $no_sgid directory(ies) without setgid, which is what keeps new files in the group

Read these, then run them:

  sudo find "$library" -xdev -name lost+found -prune -o ! -type l ! -group $gid -exec chgrp $gid -- {} +
  sudo find "$library" -xdev -name lost+found -prune -o ! -type l ! -perm -g+w -exec chmod g+rwX -- {} +
  sudo find "$library" -xdev -name lost+found -prune -o -type d ! -perm -g+s -exec chmod g+s -- {} +
EOF
fi

echo
echo "Then start the stack again: docker compose up -d"
