# AlForks — Changelog

## 2026.06.24.2
- **New Glossary** — a plain-language reference for the terms you see around the
  app. What **riding time** means versus **elapsed time**, **climbing** versus
  **vertical**, what an **assisted** lift-or-shuttle segment is, how your
  **recent records** differ from **all-time** ones, and what the training numbers
  (**Fitness, Fatigue, Form, VO₂max** and **heart-rate zones**) actually tell you.
  Find it under the **⚙ menu → Glossary**.

## 2026.06.24.1
- **Redesigned Dashboard** — the home page now leads with what actually matters
  at a glance. Your **last 365 days** sit up top — distance, moving time,
  climbing, descent, days out and activities — each with an **up/down arrow vs.
  the year before**, so you can see at once whether you're ahead of last year.
- **Year-over-year chart** — one chart across all your sports, month by month,
  this year against previous years. Switch between distance, time, climbing and
  activity count, and pick which years to compare.
- **Records, recent next to all-time** — the personal bests you set in the
  **last 365 days** now sit right beside your **all-time** bests, per sport, so a
  strong recent result is easy to spot against your lifetime best.
- **Activity ribbon up top** — your year shown as a single colour-coded strip,
  one block per day with today on the right; hover any day to see what you did.
- **Drill into a sport** — open any sport for its own year-over-year chart,
  average/top speed, and its recent and all-time records.

## 2026.06.23.5
- **New Training page** — rebuilt around plain questions instead of jargon:
  *What should I do next? · Am I getting fitter? · Am I overdoing it? · Am I
  training right?* Each gives a one-line answer with the trends behind it.
- **Every ride counts** — your training load works with or without heart rate,
  so the picture isn't blank just because a ride had no HR.
- **More with a Garmin** — if you wear one, it folds in VO₂max, resting HR, Body
  Battery and sleep for a "should I go hard today?" read, and reminds you to keep
  your weight up to date.
- Sharper heart-rate zones, a "fresh or buried?" fitness-vs-fatigue chart, your
  easy-effort-HR fitness trend, and a body-weight tracker (log it from Settings →
  Fitness or the Training page).

## 2026.06.23.4
- **Heart rate shows immediately after updating** — the previous fix restored
  heart rate, but pages you'd already viewed could still be cached without it.
  This clears those stale views so HR appears right away on your rides and
  summaries (no manual refresh needed).

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
