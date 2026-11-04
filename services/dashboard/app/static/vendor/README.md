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

## driver.js.iife.js, driver.css

- Library: driver.js
- Version: 1.10.2
- Source: the npm package `driver.js@1.10.2` (`dist/driver.js.iife.js`, `dist/driver.css`); its tarball matches npm's published integrity, and both files match cdnjs's published SRI for 1.10.2
- SHA-512 `driver.js.iife.js`: `sha512-O6kxUA71TXz258WBnBzYeyOWsZkrZPeC1ng18y8Eh5OsjbbFJFRwnr87ArUIWDYU7FSNJOh4dLHNSAy1nNMtww==`
- SHA-512 `driver.css`: `sha512-GKHrNF1gHPB/uV1/K1zKnuJzT4UBLOgzZaCwGbBHuwfSHDzAoRk04AvAqudpE7A39vp+rytS2NWPL9gPfs0ooA==`
- Licence: MIT (Kamran Ahmed)
- Used by the guided tours (`static/tour.js`) on the home and review pages; served only to browsers.

### How to update

1. Download both files for the new version and compute their hashes:

   ```
   for f in driver.js.iife.js driver.css; do
     curl -fsSL -o services/dashboard/app/static/vendor/$f https://cdnjs.cloudflare.com/ajax/libs/driver.js/<version>/$f
     echo "$f sha512-$(openssl dgst -sha512 -binary services/dashboard/app/static/vendor/$f | openssl base64 -A)"
   done
   ```

2. Compare with cdnjs's published values: `curl -fsSL 'https://api.cdnjs.com/libraries/driver.js/<version>?fields=sri'`.
3. Update the version and hashes above, the `?v=` on the driver.js tags in `templates/index.html` and `templates/review.html`, and the version pinned in `tests/test_booth_tours.py`.
