# AlForks

A local web app to browse, map, tag, and analyse your GPS activity tracks
(mountain biking, hiking, skiing) pulled from **Strava** (with optional **Garmin**
heart-rate). It runs entirely on your own Windows PC — nothing is uploaded
anywhere except your own requests to Strava and (optionally) Mapbox.

## Get started (Windows)

1. Install **Python 3.10+** — <https://www.python.org/downloads/> (tick **"Add Python to PATH"** during install).
2. Get the app:
   ```
   git clone https://github.com/bdoarowberry/alforks.git
   ```
3. Open the `alforks` folder and double-click **`start.bat`**. It sets everything
   up (first run takes a minute) and opens your browser at <http://localhost:5000>.

The in-app **Setup & Help guide** opens automatically on first run (also at
`/guide`) and walks you through connecting Strava (and Garmin, if you have one).

To stop the app, close the command window. To start it again, run `start.bat`.

## Updating

`start.bat` checks GitHub each time it launches and pulls the latest version
automatically — so you're always current with nothing to do. (Prefer manual?
Close the app and run `git pull`.) Your rides, settings, and logins live outside
version control and are never touched by an update.

See the [wiki](https://github.com/bdoarowberry/alforks/wiki) for full setup help,
or `CHANGELOG.md` for what's in each release.
