# AlForks — Changelog

## 2026.06.23.3
- **Heart rate is back** — on Windows, missing timezone data was making heart
  rate (and HR zones, training load and weather) silently disappear from rides.
  Your HR was always safe on disk; this restores it. After updating, heart rate
  shows up again on your activities.

## 2026.06.23.2
- **Cleaner navigation** — the top bar is now five focused tabs (Dashboard,
  Activities, Map, Training, Trails & Routes), with a **⚙ menu** in the corner for
  Settings, Clean-up, Help and What's New. A few pages were renamed to say what
  they do: Summary → Dashboard, Logs → Activities, Heat Map → Map, Setup →
  Settings, Review → Clean-up. **Routes** is now easy to reach next to Trails.
- **Notifications bell** — a 🔔 in the header shows a count when there's something
  worth a look: rides to clean up, or a new update with release notes. Open it to
  read each one and dismiss it with ×.
- **Friendlier messages** — when a sync runs into trouble it now explains what
  happened and what to do in plain language (and points you to Settings to
  reconnect) instead of showing a technical error. A brand-new copy now shows a
  clear "connect Strava to get started" screen instead of an empty dashboard.
- The **Sync** and **Upload** buttons are now compact icons (↻ / ↥) — hover for a
  tooltip.

## 2026.06.23.1
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
