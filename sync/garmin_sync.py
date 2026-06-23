"""Garmin Connect HR sync for AlForks GPX viewer.

One-time setup:
    pip install garminconnect
    python garmin_sync.py --login

Thereafter:
    python garmin_sync.py --sync     # pull HR for dates missing from cache
    python garmin_sync.py --resync   # re-fetch everything (rarely needed)
    python garmin_sync.py --status   # show sync state

Credentials / tokens live OUTSIDE the OneDrive-synced project folder,
at ~/.alforks/, so they won't be pushed to any cloud.
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from _common import secure_chmod as _secure_chmod

# ─── Layout ───────────────────────────────────────────────────────────────────
# Project root is the parent of the sync/ folder this script lives in.
_ROOT         = Path(__file__).resolve().parent.parent
GPX_DIR       = _ROOT / "tracks"
CACHE_DIR     = _ROOT / "cache"
HR_CACHE_DIR  = CACHE_DIR / "hr"

# Outside the OneDrive folder so tokens don't sync to the cloud
TOKEN_DIR        = Path(os.environ.get("ALFORKS_HOME") or (Path.home() / ".alforks"))
STATUS_FILE      = TOKEN_DIR / "garmin_status.json"
GARMIN_CREDS_FILE = TOKEN_DIR / "garmin_creds.txt"


def _read_creds_file() -> tuple[str, str] | None:
    """Read email/password from GARMIN_CREDS_FILE.
    Format — one `key=value` per line:
        email=you@example.com
        password=yourpassword
    Returns (email, password) if both present and non-empty, else None.
    """
    if not GARMIN_CREDS_FILE.exists():
        return None
    creds = {}
    for line in GARMIN_CREDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip().lower()] = v.strip()
    email, pw = creds.get("email", ""), creds.get("password", "")
    return (email, pw) if email and pw else None


def _ensure_library():
    """Import garminconnect lazily so --status/--help work without it installed."""
    try:
        from garminconnect import Garmin          # noqa: F401
    except ImportError:
        print("Missing dependency. Install with:\n  pip install garminconnect")
        sys.exit(1)


# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_client(interactive: bool = False):
    """Return an authenticated Garmin client.

    Tries stored tokens first. If those fail and `interactive` is True,
    prompts for email / password (and MFA if the library asks).
    """
    _ensure_library()
    from garminconnect import Garmin

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _secure_chmod(TOKEN_DIR, 0o700)

    # Fast path: use stored tokens
    try:
        g = Garmin()
        g.login(tokenstore=str(TOKEN_DIR))
        return g
    except Exception as e:
        if not interactive:
            raise RuntimeError(
                f"Garmin auth not available ({e}). "
                "Run `python garmin_sync.py --login` first."
            )

    # Slow path: fresh login with credentials
    creds = _read_creds_file()
    if creds:
        email, password = creds
        print(f"Fresh Garmin login as {email} (from {GARMIN_CREDS_FILE}).\n")
    else:
        print("Fresh Garmin login required.")
        print(f"(Tip: put email=... and password=... in {GARMIN_CREDS_FILE} to skip prompts next time.)\n")
        email    = input("Garmin email: ").strip()
        password = getpass.getpass("Garmin password (hidden as you type): ")
    g = Garmin(email=email, password=password)
    try:
        g.login()
    except Exception as e:
        print(f"\nLogin failed: {e}")
        sys.exit(1)

    # Persist tokens for future silent use
    try:
        g.client.dump(str(TOKEN_DIR))
        print(f"  Tokens saved to: {TOKEN_DIR}")
    except Exception as e:
        print(f"  Warning: failed to save tokens ({e}). Future syncs will need to log in again.")

    return g


# ─── Date discovery ───────────────────────────────────────────────────────────

def activity_dates() -> list[str]:
    """Return sorted unique 'YYYY-MM-DD' strings for every GPX file."""
    return sorted(activity_windows().keys())


_TIME_RE = re.compile(r"<time>([^<]+)</time>")


def activity_windows() -> dict[str, list[tuple[int, int]]]:
    """For each YYYY-MM-DD, list of (start_ms, end_ms) UTC tuples — one per
    activity recorded that date. Used to decide whether the cached HR for a
    date actually covers every activity on it.

    Reads only the first 4 KB and last 4 KB of each GPX file so we never have
    to parse multi-MB tracks just to find their start/end timestamps.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for path in sorted(GPX_DIR.glob("*.gpx")):
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                head = f.read(4096).decode("utf-8", errors="ignore")
                if size > 8192:
                    f.seek(max(0, size - 4096))
                    tail = f.read().decode("utf-8", errors="ignore")
                else:
                    tail = head
            head_times = _TIME_RE.findall(head)
            tail_times = _TIME_RE.findall(tail)
            if not head_times or not tail_times:
                continue
            start_iso = head_times[0]
            end_iso   = tail_times[-1]
            start_dt  = datetime.fromisoformat(start_iso)
            end_dt    = datetime.fromisoformat(end_iso)
            date_str  = start_iso[:10]
            start_ms  = int(start_dt.timestamp() * 1000)
            end_ms    = int(end_dt.timestamp() * 1000)
            out.setdefault(date_str, []).append((start_ms, end_ms))
        except Exception:
            continue
    return out


# ─── HR cache ─────────────────────────────────────────────────────────────────

def hr_cache_path(date_str: str) -> Path:
    return HR_CACHE_DIR / f"{date_str}.json"


def missing_dates(dates: list[str]) -> list[str]:
    return [d for d in dates if not hr_cache_path(d).exists()]


_HR_COVERAGE_TOL_MS = 5 * 60 * 1000

# We give Garmin this many days from the activity to ingest the watch's HR
# upload. Within the window an empty-sample cache is considered incomplete
# and gets re-fetched on every --sync (so a delayed manual watch-sync gets
# picked up automatically). After the window expires we trust the empty
# cache as "Garmin really has no HR for this date" and stop retrying.
# --retry-empty re-fetches all empties regardless of age; --retry-date
# re-fetches one specific date.
_EMPTY_GIVE_UP_AFTER_DAYS = 7


def _hr_covers(samples: list, start_ms: int, end_ms: int) -> bool:
    if not samples:
        return False
    return samples[0][0] <= start_ms + _HR_COVERAGE_TOL_MS \
       and samples[-1][0] >= end_ms - _HR_COVERAGE_TOL_MS


def _date_is_complete(date_str: str, windows: list[tuple[int, int]],
                      trust_stale_empty: bool = True) -> bool:
    """True if the cached HR for date_str spans every activity window on it.

    Empty caches inside `_EMPTY_GIVE_UP_AFTER_DAYS` of the activity count as
    incomplete — `--sync` will re-fetch them so delayed watch-uploads get
    picked up. Past the cutoff we trust the empty as genuinely-no-data and
    stop retrying. `trust_stale_empty=False` (used by `--retry-empty`)
    forces all empties to count as incomplete regardless of age."""
    path = hr_cache_path(date_str)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        samples = payload.get("samples") or []
    except Exception:
        return False
    if not samples:
        if not trust_stale_empty:
            return False
        try:
            activity_date = datetime.fromisoformat(date_str).date()
            days_since = (datetime.now().date() - activity_date).days
        except ValueError:
            return True
        return days_since >= _EMPTY_GIVE_UP_AFTER_DAYS
    return all(_hr_covers(samples, s, e) for s, e in windows)


def incomplete_dates(windows: dict[str, list[tuple[int, int]]],
                     trust_stale_empty: bool = True) -> list[str]:
    """Dates whose HR cache is missing OR has samples that don't cover every
    activity window. A cache fetched mid-day that stops before an activity
    ended counts as incomplete and must be re-fetched."""
    return sorted(d for d, wins in windows.items()
                  if not _date_is_complete(d, wins, trust_stale_empty))


def fetch_and_cache_hr(client, date_str: str) -> tuple[bool, int, str | None]:
    """Fetch HR for one date. Returns (success, sample_count, error_or_None).

    The error string lets the caller tell a systemic failure (rate-limit, auth,
    network) apart from an ordinary "no data" so a throttled run doesn't report
    false success."""
    try:
        data = client.get_heart_rates(date_str)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"  {date_str}: fetch failed ({err})")
        return False, 0, err

    samples = (data or {}).get("heartRateValues") or []
    payload = {
        "date":    date_str,
        "samples": samples,   # list of [utc_ms, bpm]
        "resting": (data or {}).get("restingHeartRate"),
        "max_day": (data or {}).get("maxHeartRate"),
        "min_day": (data or {}).get("minHeartRate"),
        "fetched": int(time.time()),
    }

    HR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = hr_cache_path(date_str).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(hr_cache_path(date_str))
    return True, len(samples), None


# ─── Status file ──────────────────────────────────────────────────────────────

def read_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def update_status(**fields):
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _secure_chmod(TOKEN_DIR, 0o700)
    s = read_status()
    s.update(fields)
    STATUS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def is_configured() -> bool:
    """True if stored tokens exist - doesn't validate them."""
    return TOKEN_DIR.exists() and any(TOKEN_DIR.glob("oauth*.json"))


# ─── CLI commands ─────────────────────────────────────────────────────────────

def cmd_login():
    g = get_client(interactive=True)
    try:
        name = g.get_full_name()
    except Exception:
        name = "(unknown)"
    update_status(last_login=int(time.time()), user=name)
    print(f"\n[OK] Logged in as: {name}")
    print(f"  Tokens saved to: {TOKEN_DIR}")


def _emit_progress(phase: str, done: int, total: int) -> None:
    """Machine-readable progress line the app's sync runner parses for the
    per-phase status UI. Plain stdout; ignored by anything that doesn't grok it."""
    print(f"@@PROGRESS {json.dumps({'phase': phase, 'done': done, 'total': total})}",
          flush=True)


def systemic_failure_message(failures: int, synced_ok: int,
                             last_err: str) -> str | None:
    """If EVERY date failed, return a user-facing reason classified from the last
    error (rate-limit vs auth vs network) so the caller can exit non-zero. Returns
    None when the run isn't a total failure (partial success is still success).
    The wording embeds keywords the app's _friendly_sync_message routes on."""
    if not (failures and synced_ok == 0):
        return None
    low = (last_err or "").lower()
    if any(k in low for k in ("toomanyrequests", "too many requests", "429",
                              "rate limit", "ratelimit")):
        return ("Garmin is rate-limiting requests (HTTP 429 Too Many Requests) "
                "— wait a few minutes and sync again.")
    if any(k in low for k in ("auth", "401", "unauthorized", "login",
                              "credential")):
        return ("Garmin auth failed — not logged in. Reconnect Garmin on the "
                "Setup page.")
    return (f"Couldn't reach Garmin — all {failures} date(s) failed. Check your "
            f"connection and try again. (last error: {last_err})")


def cmd_sync(only_missing: bool = True, throttle: float = 1.0,
             retry_empty: bool = False):
    client = get_client(interactive=False)
    windows  = activity_windows()
    all_dates = sorted(windows.keys())
    if retry_empty:
        # Force every empty-sample cache to re-fetch, regardless of age.
        # Useful after a delayed manual watch-sync, when the trust window
        # would otherwise still consider those caches "fresh enough."
        dates = incomplete_dates(windows, trust_stale_empty=False)
    elif only_missing:
        dates = incomplete_dates(windows)
    else:
        dates = all_dates
    if not dates:
        print(f"Nothing to sync - all {len(all_dates)} activity dates fully covered.")
        update_status(last_sync=int(time.time()), last_synced=0, total_dates=len(all_dates))
        return

    missing = sum(1 for d in dates if not hr_cache_path(d).exists())
    incomplete = len(dates) - missing
    breakdown = []
    if missing:    breakdown.append(f"{missing} missing")
    if incomplete: breakdown.append(f"{incomplete} incomplete")
    print(f"Syncing {len(dates)} date{'s' if len(dates) != 1 else ''} "
          f"({', '.join(breakdown) or 'all'}; {len(all_dates)} total activity dates)...")
    _emit_progress("hr", 0, len(dates))
    synced_ok = 0
    total_samples = 0
    failures = 0
    last_err = ""
    for i, d in enumerate(dates, 1):
        ok, n, err = fetch_and_cache_hr(client, d)
        if ok:
            synced_ok += 1
            total_samples += n
            tag = f"{n} samples" if n else "no HR data"
            print(f"  [{i}/{len(dates)}] {d}  [OK] {tag}")
        else:
            failures += 1
            last_err = err or last_err
        time.sleep(throttle)
        _emit_progress("hr", i, len(dates))

    update_status(
        last_sync    = int(time.time()),
        last_synced  = synced_ok,
        total_dates  = len(all_dates),
    )

    # A run where EVERY date failed is a systemic problem (rate-limit, expired
    # auth, network) — not a successful "0 new dates". Exit non-zero so the GUI
    # surfaces an error instead of a false success. Partial failures still count
    # as success — some HR landed.
    fail_msg = systemic_failure_message(failures, synced_ok, last_err)
    if fail_msg:
        sys.exit(fail_msg)

    suffix = f" ({failures} failed)" if failures else ""
    print(f"\n[OK] Synced {synced_ok} / {len(dates)} dates "
          f"({total_samples:,} HR samples){suffix}.")


def cmd_retry_date(date_str: str):
    """Re-fetch HR for a single date — escape hatch when --sync's give-up
    window has passed but you know your watch has data for that date now."""
    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        print(f"Bad date format: {date_str!r}. Expected YYYY-MM-DD.")
        sys.exit(2)
    client = get_client(interactive=False)
    print(f"Re-fetching HR for {date_str}...")
    ok, n, _err = fetch_and_cache_hr(client, date_str)
    if ok:
        tag = f"{n} samples" if n else "no HR data"
        print(f"  [OK] {tag}")
        update_status(last_sync=int(time.time()), last_synced=1)
    else:
        sys.exit(1)


def cmd_status():
    s = read_status()
    tokens = "present" if is_configured() else "missing - run --login"
    print(f"Token dir:        {TOKEN_DIR}")
    print(f"Library tokens:   {tokens}")
    user = s.get("user")
    if user:
        print(f"Account:    {user}")
    if s.get("last_login"):
        print(f"Last login: {datetime.fromtimestamp(s['last_login']).isoformat(timespec='seconds')}")
    if s.get("last_sync"):
        print(f"Last sync:  {datetime.fromtimestamp(s['last_sync']).isoformat(timespec='seconds')}"
              f"  (synced {s.get('last_synced', 0)} dates)")

    # Cache summary. Buckets are mutually exclusive so the four counts
    # always sum to len(all_dates):
    #   fully_covered  — non-empty cache, samples span every activity window
    #   incomplete     — cache has samples but doesn't span (or is a stale
    #                    empty awaiting auto-retry on next --sync)
    #   trusted_empty  — empty cache past the 7-day give-up window;
    #                    presumed Garmin genuinely has no HR for that date
    #   missing        — no cache file at all
    windows      = activity_windows()
    all_dates    = sorted(windows.keys())
    fully_covered = trusted_empty = incomplete = missing = 0
    for d in all_dates:
        path = hr_cache_path(d)
        if not path.exists():
            missing += 1
            continue
        if _date_is_complete(d, windows[d]):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("samples"):
                    fully_covered += 1
                else:
                    trusted_empty += 1
            except Exception:
                incomplete += 1
        else:
            incomplete += 1
    print(f"\nCoverage: {len(all_dates) - missing} / {len(all_dates)} activity dates cached")
    print(f"  fully covered:               {fully_covered}")
    print(f"  incomplete (will retry):     {incomplete}")
    print(f"  empty (trusted, no retry):   {trusted_empty}")
    print(f"  missing:                     {missing}")


# ─── Entry ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Garmin HR sync for AlForks.")
    p.add_argument("--login",       action="store_true", help="One-time Garmin login. Reads creds from ~/.alforks/garmin_creds.txt or prompts.")
    p.add_argument("--sync",        action="store_true", help="Pull HR for missing dates.")
    p.add_argument("--retry-empty", action="store_true", help="Re-fetch every empty-cache date regardless of age. Useful after a delayed manual watch-sync when an old empty has fallen past the 7-day give-up window.")
    p.add_argument("--retry-date",  metavar="YYYY-MM-DD",   help="Re-fetch HR for a single date. Use when you know one specific track is missing data.")
    p.add_argument("--resync",      action="store_true", help="Re-fetch every activity date (overwrites cache).")
    p.add_argument("--status",      action="store_true", help="Show auth + sync status.")
    args = p.parse_args()

    if args.login:                cmd_login()
    elif args.sync:               cmd_sync(only_missing=True)
    elif args.retry_empty:        cmd_sync(only_missing=True, retry_empty=True)
    elif args.retry_date:         cmd_retry_date(args.retry_date)
    elif args.resync:             cmd_sync(only_missing=False)
    elif args.status:             cmd_status()
    else:                         p.print_help()


if __name__ == "__main__":
    main()
