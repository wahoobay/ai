# Vendored third-party scripts

## hls.min.js

- Library: hls.js
- Version: 1.6.16
- Source: https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.6.16/hls.min.js
- SHA-512 (matches cdnjs's published SRI): `sha512-zAODd1LM+/58SazX5fct5psehCijY60y5rG0BYIzxTXot7ovEo2FhfWTV0mp4oqIef2xriyg4zlrPlmFTDcPXg==`
- Licence: Apache-2.0
- Used by the live panel when `LIVE_HLS_BASE_URL` is set; served only to browsers.

### How to update

1. Download the new version and compute its hash:

   ```
   curl -fsSL -o services/dashboard/app/static/vendor/hls.min.js https://cdnjs.cloudflare.com/ajax/libs/hls.js/<version>/hls.min.js
   openssl dgst -sha512 -binary services/dashboard/app/static/vendor/hls.min.js | openssl base64 -A
   ```

2. Compare with cdnjs's published value: `curl -fsSL 'https://api.cdnjs.com/libraries/hls.js/<version>?fields=sri'` (the `hls.min.js` entry, without the `sha512-` prefix).
3. Update the version, URL and hash above, the `?v=` on the hls.js script tag in `templates/index.html`, and the version pinned in `tests/test_live_player.py`.
