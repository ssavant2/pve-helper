# Vendored front-end assets

These third-party browser libraries are pinned and served locally instead of
from a CDN, so the browser only ever talks to pve-helper (see the console
security model). They are **not** managed by `requirements.txt` — when you bump
other components, check whether these need updating too, and re-vendor them by
hand.

| File | Package @ version | Source (jsDelivr) |
|------|-------------------|-------------------|
| `novnc/rfb.esm.js` | `@novnc/novnc@1.7.0` | `https://cdn.jsdelivr.net/npm/@novnc/novnc@1.7.0/core/rfb.js/+esm` |
| `xterm/xterm.min.js` | `@xterm/xterm@6.0.0` | `https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/lib/xterm.min.js` |
| `xterm/addon-fit.min.js` | `@xterm/addon-fit@0.11.0` | `https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.min.js` |
| `xterm/xterm.min.css` | `@xterm/xterm@6.0.0` | `https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/css/xterm.min.css` |
| `lucide.min.js` | `lucide` (icons) | vendored snapshot |

The URLs these are wired to are set in `core/views/guests.py` (`console_*_url`).

## Updating (example: bump noVNC)

```sh
# noVNC is a multi-file ES module; use jsDelivr's /+esm to get one bundled file.
curl -o static/vendor/novnc/rfb.esm.js \
  "https://cdn.jsdelivr.net/npm/@novnc/novnc@<version>/core/rfb.js/+esm"

# xterm ships single UMD files:
curl -o static/vendor/xterm/xterm.min.js \
  "https://cdn.jsdelivr.net/npm/@xterm/xterm@<version>/lib/xterm.min.js"
curl -o static/vendor/xterm/addon-fit.min.js \
  "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@<version>/lib/addon-fit.min.js"
curl -o static/vendor/xterm/xterm.min.css \
  "https://cdn.jsdelivr.net/npm/@xterm/xterm@<version>/css/xterm.min.css"
```

After downloading, strip any trailing `//# sourceMappingURL=...` comment (the
`.map` files are not vendored), bump the version numbers in this table and in
`core/views/guests.py`, then rebuild the web image so `collectstatic` picks the
files up.
