# AlForks

A local web app to browse, map, tag, and analyse your GPS activity tracks
(mountain biking, hiking, skiing) pulled from Strava. It runs entirely on your
own Windows PC — nothing is uploaded anywhere except your own requests to Strava
and (optionally) Mapbox.

## Quick start

1. Make sure **Python 3.10+** is installed
   (https://www.python.org/downloads/ — tick **"Add Python to PATH"** during install).
2. Double-click **`start.bat`**. It sets everything up and opens your browser at
   http://localhost:5000.
3. The in-app **Setup & Help guide** opens automatically on first run (also at
   `/guide`) and walks you through connecting your own Strava.

To stop the app, close the command window. To start it again, run `start.bat`.

## Updating

If you receive an `alforks-update-*.zip`: close the app, drop the zip into this
folder, and double-click **`update.bat`**. Your rides, settings, regions, and
Strava login are left untouched.

## For the owner (making copies)

Run from the repo root (Python, not PowerShell — this machine's AllSigned policy
blocks unsigned `.ps1`):

```
python make_friend_copy.py --destination ../AlForks-for-bob   # full clean copy to share
python make_friend_copy.py --package                          # build an update bundle
```

Clean copies exclude all personal data and secrets (tracks, cache, metadata,
weights, your Strava login, your Mapbox token) and include your `regions.json`
and `types.json` as starter settings. Update bundles (`--package`) are code-only,
so a recipient's customisations and data always survive an update.

See `CHANGELOG.md` for what's in each release.
