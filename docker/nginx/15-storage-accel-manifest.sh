#!/bin/sh
set -eu

manifest=/storage-accel-state/mounts
temporary="${manifest}.tmp"

mkdir -p "$(dirname "$manifest")"
awk '
  {
    mountpoint = $5
    gsub(/\\040/, " ", mountpoint)
    if (index(mountpoint, "/storages/") == 1) {
      relative = substr(mountpoint, length("/storages/") + 1)
      split(relative, parts, "/")
      if (parts[1] != "") print parts[1]
    }
  }
' /proc/self/mountinfo | sort -u > "$temporary"
chmod 0444 "$temporary"
mv "$temporary" "$manifest"
