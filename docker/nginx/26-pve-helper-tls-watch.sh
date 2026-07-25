#!/bin/sh
set -eu

# Reload nginx when the application publishes a different certificate.
#
# The alternative was telling the operator to run `docker compose restart nginx`
# after every certificate change, which turns a UI action into a shell action on the
# Docker host and guarantees that at some point someone selects a renewed certificate
# and walks away believing it is live. The application cannot reload nginx itself
# without a Docker socket, and handing the web application a Docker socket to solve a
# file-watching problem trades a small inconvenience for root on the host.
#
# So the watcher lives here, on the nginx side, and polls one small file. The
# application writes `state` last and atomically, after the certificate and key are
# fully in place, so a changed digest means the material behind it is already
# complete. Nothing is parsed and nothing is hashed on the loop: the common case is a
# digest that has not moved, which costs one read every interval.
#
# Backgrounded because /docker-entrypoint.d steps run to completion before nginx
# starts. The first sleep is what keeps the initial `nginx -s reload` from racing the
# master process that has not been forked yet.

state_dir="${PVE_HELPER_CERTIFICATE_STATE_DIR:-/certificate-state}"
state_file="${state_dir}/state"
interval="${PVE_HELPER_CERTIFICATE_WATCH_SECONDS:-15}"

read_state() {
  cat "$state_file" 2>/dev/null || true
}

watch() {
  previous="$(read_state)"
  while true; do
    sleep "$interval"
    current="$(read_state)"
    [ "$current" = "$previous" ] && continue
    previous="$current"
    echo "pve-helper: certificate state changed, reloading nginx"
    # Regenerate before reloading; the digest may have changed because HTTPS was
    # switched off, in which case the listener has to go away rather than keep
    # serving the certificate that is no longer on disk.
    if /docker-entrypoint.d/25-pve-helper-tls.sh && nginx -t; then
      nginx -s reload
    else
      echo "pve-helper: refusing to reload, generated configuration is not valid" >&2
    fi
  done
}

watch &
