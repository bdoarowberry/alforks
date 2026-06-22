# AlForks — Changelog

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
