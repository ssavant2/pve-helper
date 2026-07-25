#!/bin/sh
set -eu

# Turn the certificate the application published into a TLS listener.
#
# nginx cannot read the database, so the decision made in Settings → Certificates
# arrives here as files on a shared volume: server.crt, server.key and a `state`
# digest. This script translates their presence into configuration, and is written to
# be safe to run again at any time — it is both an entrypoint step and the body of
# the reload watcher.
#
# The published port does not change; only the scheme does. That is why this writes
# the `listen` directive rather than a second server block: the application keeps the
# address the operator already has bookmarked, an external reverse proxy keeps
# pointing at the same port, and there is no second port to allocate, publish or
# collide with a neighbouring stack. A browser that arrives over plain HTTP is
# answered by nginx's own 497 and redirected to the same authority over TLS.
#
# One deliberate refusal to enable HTTPS: APP_FORCE_HTTP. Turning the only port over
# to TLS is exactly the change a wrong certificate makes unrecoverable from the UI,
# so the way back in must not depend on reaching the application it is recovering.

state_dir="${PVE_HELPER_CERTIFICATE_STATE_DIR:-/certificate-state}"
conf_dir=/etc/nginx/conf.d
certificate="${state_dir}/server.crt"
key="${state_dir}/server.key"
listen_include="${conf_dir}/pve-helper-listen.include"

write_atomic() {
  target="$1"
  temporary="${target}.tmp"
  cat > "$temporary"
  mv "$temporary" "$target"
}

serve_plain_http() {
  echo "listen 80;" | write_atomic "$listen_include"
}

if [ "${APP_FORCE_HTTP:-}" = "true" ] || [ "${APP_FORCE_HTTP:-}" = "1" ]; then
  serve_plain_http
  echo "pve-helper: APP_FORCE_HTTP is set, serving plain HTTP only"
  exit 0
fi

if [ ! -s "$certificate" ] || [ ! -s "$key" ]; then
  serve_plain_http
  exit 0
fi

write_atomic "$listen_include" <<CONFIGURATION
listen 80 ssl;
http2 on;

ssl_certificate ${certificate};
ssl_certificate_key ${key};
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:pve_helper_tls:10m;
ssl_session_timeout 1h;
ssl_session_tickets off;

# 497 is nginx's "plain HTTP request was sent to an HTTPS port". Handing it to a
# named location rather than returning a redirect inline keeps the response a
# real redirect instead of an error page, so a bookmarked http:// address still
# lands on the page the operator asked for.
error_page 497 = @pve_helper_https_upgrade;
CONFIGURATION

echo "pve-helper: HTTPS enabled on the published port"
