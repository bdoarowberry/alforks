"""Garmin daily-wellness sync for the Training page.

Pulls the signals a Forerunner 245 actually provides — VO2max + training status
(from activities recorded on the watch), and the 24/7 daily-wellness metrics
Body Battery, resting HR, sleep and stress (the reliable recovery signals, since
the 245 has NO Training Readiness or HRV Status). One small summary per date is
cached in cache/wellness/<date>.json; the heavy raw payloads are discarded.

Mirrors garmin_sync.py's incremental, throttled pattern and reuses its client.
CLI:  python garmin_wellness.py --sync [--days N]
"""
import json
import sys
import time

import garmin_sync   # reuse get_client / CACHE_DIR / throttle / progress

WELLNESS_CACHE_DIR = garmin_sync.CACHE_DIR / "wellness"


# ─── Extraction (pure — unit-tested) ────────────────────────────────────────

def _latest_device_status(ts: dict) -> dict:
    """Pick the most recent per-device entry from latestTrainingStatusData."""
    data = (ts or {}).get("latestTrainingStatusData") or {}
    best = {}
    for v in data.values():
        if not isinstance(v, dict):
            continue
        if (v.get("timestamp") or 0) >= (best.get("timestamp") or 0):
            best = v
    return best


def extract_wellness(date: str, ts=None, bb=None, rhr=None,
                     sleep=None, stress=None, im=None) -> dict:
    """Reduce the (large) Garmin payloads to a small per-date summary dict.
    Every field is optional — a missing/empty payload just leaves it absent."""
    out: dict = {"date": date}

    # VO2max + fitness age (running-based on a 245; updates when worn for a run)
    gen = ((ts or {}).get("mostRecentVO2Max") or {}).get("generic") or {}
    if gen.get("vo2MaxValue") is not None:
        out["vo2max"] = round(float(gen["vo2MaxValue"]), 1)
        out["vo2max_date"] = gen.get("calendarDate")
        if gen.get("fitnessAge") is not None:
            out["fitness_age"] = int(gen["fitnessAge"])

    # Training status (0 = none/undetermined; Garmin's enum otherwise)
    dev = _latest_device_status(ts)
    if dev:
        if dev.get("trainingStatus") is not None:
            out["training_status"] = int(dev["trainingStatus"])
        if dev.get("weeklyTrainingLoad") is not None:
            out["weekly_training_load"] = int(dev["weeklyTrainingLoad"])
        if dev.get("loadTunnelMin") is not None:
            out["load_tunnel_min"] = int(dev["loadTunnelMin"])
        if dev.get("loadTunnelMax") is not None:
            out["load_tunnel_max"] = int(dev["loadTunnelMax"])

    # Body Battery — daily charge/drain + high/low/end level
    day = None
    if isinstance(bb, list) and bb:
        day = bb[0]
    elif isinstance(bb, dict):
        day = bb
    if isinstance(day, dict):
        if day.get("charged") is not None:
            out["bb_charged"] = int(day["charged"])
        if day.get("drained") is not None:
            out["bb_drained"] = int(day["drained"])
        levels = [e[-1] for e in (day.get("bodyBatteryValuesArray") or [])
                  if isinstance(e, (list, tuple)) and e and e[-1] is not None]
        if levels:
            out["bb_high"] = max(levels)
            out["bb_low"] = min(levels)
            out["bb_end"] = levels[-1]

    # Resting HR
    m = (((rhr or {}).get("allMetrics") or {}).get("metricsMap") or {}) \
        .get("WELLNESS_RESTING_HEART_RATE") or []
    if m and isinstance(m[0], dict) and m[0].get("value") is not None:
        out["resting_hr"] = int(m[0]["value"])

    # Sleep — duration + stages (245 gives stages but no 0-100 sleep score)
    ds = (sleep or {}).get("dailySleepDTO") or {}
    if ds.get("sleepTimeSeconds") is not None:
        out["sleep_sec"] = int(ds["sleepTimeSeconds"])
        for k_src, k_dst in (("deepSleepSeconds", "sleep_deep"),
                             ("lightSleepSeconds", "sleep_light"),
                             ("remSleepSeconds", "sleep_rem"),
                             ("awakeSleepSeconds", "sleep_awake")):
            if ds.get(k_src) is not None:
                out[k_dst] = int(ds[k_src])
        score = (ds.get("sleepScores") or {}).get("overall") or {}
        if score.get("value") is not None:
            out["sleep_score"] = int(score["value"])

    # All-day stress
    if (stress or {}).get("avgStressLevel") is not None:
        out["stress_avg"] = int(stress["avgStressLevel"])
    if (stress or {}).get("maxStressLevel") is not None:
        out["stress_max"] = int(stress["maxStressLevel"])

    # Intensity minutes (weekly running total)
    if (im or {}).get("weeklyModerate") is not None:
        out["im_moderate_wk"] = int(im["weeklyModerate"])
    if (im or {}).get("weeklyVigorous") is not None:
        out["im_vigorous_wk"] = int(im["weeklyVigorous"])

    return out


def wellness_path(date_str: str):
    return WELLNESS_CACHE_DIR / f"{date_str}.json"


# ─── Fetch + cache ──────────────────────────────────────────────────────────

def fetch_and_cache(client, date_str: str) -> tuple[bool, str | None]:
    """Fetch all wellness endpoints for one date, extract, cache. Returns
    (ok, error). Individual endpoint failures are tolerated (left absent); only
    a hard failure (auth/rate-limit/network on every call) returns ok=False."""
    payloads = {}
    errs = 0
    last_err = None
    calls = {
        "ts":     lambda: client.get_training_status(date_str),
        "bb":     lambda: client.get_body_battery(date_str, date_str),
        "rhr":    lambda: client.get_rhr_day(date_str),
        "sleep":  lambda: client.get_sleep_data(date_str),
        "stress": lambda: client.get_stress_data(date_str),
        "im":     lambda: client.get_intensity_minutes_data(date_str),
    }
    for key, fn in calls.items():
        try:
            payloads[key] = fn()
        except Exception as e:
            errs += 1
            last_err = f"{type(e).__name__}: {e}"
            payloads[key] = None
    if errs == len(calls):
        return False, last_err   # nothing came back — systemic failure

    summary = extract_wellness(date_str, ts=payloads["ts"], bb=payloads["bb"],
                               rhr=payloads["rhr"], sleep=payloads["sleep"],
                               stress=payloads["stress"], im=payloads["im"])
    summary["fetched"] = int(time.time())
    WELLNESS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = wellness_path(date_str).with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    tmp.replace(wellness_path(date_str))
    # "Meaningful" if we got at least one real signal beyond the date stamp.
    return (len(summary) > 2), last_err


def _recent_dates(days: int) -> list[str]:
    """Calendar dates for the trailing `days`, oldest first (today excluded —
    today's wellness isn't complete yet)."""
    from datetime import date, timedelta
    today = date.today()
    return [(today - timedelta(days=n)).isoformat() for n in range(days, 0, -1)]


def cmd_sync(days: int = 120, throttle: float = 1.2, refresh_recent: int = 3,
            max_new: int | None = None, client=None):
    """Incrementally cache wellness for the trailing `days`. Skips dates already
    cached except the most recent `refresh_recent` (which may have filled in
    after the fact). `max_new` caps how many uncached dates are fetched in one
    run (most-recent first) so a first-time backfill is spread over several syncs
    and never blows the caller's timeout. Throttled + rate-limit aware."""
    if client is None:
        client = garmin_sync.get_client(interactive=False)
    dates = _recent_dates(days)
    recent_cut = set(dates[-refresh_recent:]) if refresh_recent else set()
    todo = [d for d in dates if d in recent_cut or not wellness_path(d).exists()]
    if max_new is not None and len(todo) > max_new:
        todo = todo[-max_new:]                 # newest first (todo is oldest-first)
    if not todo:
        print(f"Wellness up to date — {len(dates)} days cached.")
        return
    print(f"Syncing wellness for {len(todo)} day(s)...")
    garmin_sync._emit_progress("wellness", 0, len(todo))
    ok_n = fails = 0
    last_err = ""
    for i, d in enumerate(todo, 1):
        ok, err = fetch_and_cache(client, d)
        if ok:
            ok_n += 1
        else:
            fails += 1
            last_err = err or last_err
        time.sleep(throttle)
        garmin_sync._emit_progress("wellness", i, len(todo))

    fail_msg = garmin_sync.systemic_failure_message(fails, ok_n, last_err)
    if fail_msg:
        sys.exit(fail_msg.replace("HR", "wellness"))
    print(f"\n[OK] Wellness synced {ok_n} / {len(todo)} days"
          f"{f' ({fails} failed)' if fails else ''}.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()
    if args.sync:
        cmd_sync(days=args.days)
    else:
        ap.print_help()
