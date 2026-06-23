# AlForks — Changelog

## 2026.06.23
- **Find a ride from the map** — the Heat Map page now has a ride list down the
  right side (newest first). Hover a ride to light up its track on the map (and
  vice-versa), and click to open that ride's log.
- **Region zoom** on the Heat Map — pick a region to fly the map to it and outline
  its boundary. It only moves the camera; your rides and filters stay put.
- **What's New** page (this one, at `/whatsnew`) — the version shown in the top
  corner links here, and a banner points it out after an update.
- **Smarter duplicate detection** — possible duplicates are now matched by where
  the tracks actually go on the map. So two recordings of the *same* ride are caught
  even when their distance or time differ (gaps, pauses, different sources), while
  two *different* rides on the same day are no longer flagged just for being a
  similar length.
- Fixed the **"Not duplicates"** button, which could fail to save on some setups.

## 2026.06.21
- One-click in-app **Connect Strava** — register your own Strava app, paste
  credentials, click Connect. No terminal needed.
- One-click in-app **Connect Garmin** (optional) — email/password with
  verification-code (MFA) support. Garmin adds heart-rate to your rides; Strava
  brings in the rides themselves.
- **"Import rides from"** start date so the first sync doesn't pull years of
  history at once.
- Optional **Mapbox token** field on the Setup page (enables 3D maps).
- Redesigned, **interactive Setup & Help guide** — opens automatically on first
  run (also at `/guide`); you can connect Strava/Garmin, set the import date,
  sync, and add a Mapbox token right from the guide.
- Self-contained copies: each copy keeps its Strava login in its own folder
  (`ALFORKS_HOME`), so copies are portable.
- Distributed via **GitHub**: `git clone` to install, and `start.bat` checks for
  and pulls updates automatically on launch (`git pull --ff-only`).
