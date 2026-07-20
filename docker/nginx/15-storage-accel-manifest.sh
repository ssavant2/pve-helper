#!/bin/sh
set -eu

# Snapshot which top-level datastore directories under /storages nginx can serve,
# and *which filesystem* each one was at the time.
#
# nginx holds its /storages bind with rprivate + recursive-readonly, so it keeps its
# own copy of every submount for the container's lifetime, while the app follows the
# host through rslave. A name on its own still matches after the host unmounts an
# export and mounts a different filesystem at the same path — and then nginx serves
# bytes from the detached original while the app authorized against the replacement,
# with no error anywhere. Recording the device makes the manifest prove identity
# rather than existence.
#
# `stat -c %d` prints st_dev, the same integer Python's os.stat().st_dev returns.
# Bind mounts share the superblock, so the number is comparable across the two
# containers' mount namespaces; a remount on the host gets a new superblock and
# therefore a new number, which is exactly the case being detected. The directory is
# stat'ed rather than mountinfo parsed because it is the directory an accelerated
# URL resolves against, whether or not it is itself a mount point.

manifest=/storage-accel-state/mounts
temporary="${manifest}.tmp"

mkdir -p "$(dirname "$manifest")"
: > "$temporary"

for directory in /storages/*/; do
  [ -d "$directory" ] || continue
  name="${directory#/storages/}"
  name="${name%/}"
  # One entry per line, name and device separated by a tab. A name carrying either
  # character cannot be represented and is left out, which costs acceleration and
  # never grants it wrongly.
  case "$name" in
    *"	"* | *"
"*) continue ;;
  esac
  printf '%s\t%s\n' "$name" "$(stat -c %d "$directory")" >> "$temporary"
done

sort -u -o "$temporary" "$temporary"
chmod 0444 "$temporary"
mv "$temporary" "$manifest"
