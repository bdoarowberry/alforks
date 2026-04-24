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
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ─── Layout ───────────────────────────────────────────────────────────────────
# Project root is the parent of the sync/ folder this script lives in.
_ROOT         = Path(__file__).resolve().parent.parent
GPX_DIR       = _ROOT / "tracks"
CACHE_DIR     = _ROOT / "cache"
HR_CACHE_DIR  = CACHE_DIR / "hr"

# Outside the OneDrive folder so tokens don't sync to the cloud
TOKEN_DIR        = Path.home() / ".alforks"
STATUS_FILE      = TOKEN_DIR / "garmin_status.json"
GARMIN_CREDS_FILE = TOKEN_DIR / "garmin_creds.txt"


def _secure_chmod(path: Path, mode: int) -> None:
    """Best-effort permission hardening. Posix honours the mode; Windows
    only toggles the read-only bit and relies on ACLs for access control."""
    try:
        path.chmod(mode)
    except OSError:
        pass


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
    """Return sorted unique 'YYYY-MM-DD' strings for every GPX file.

    We only read the first timestamped track point per file - enough to get
    the date. Much faster than full gpxpy parse.
    """
    dates = set()
    import re
    date_re = re.compile(r"<time>(\d{4}-\d{2}-\d{2})")
    for path in sorted(GPX_DIR.glob("*.gpx")):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                # Read only enough to find the first <time> - avoids parsing huge files
                head = f.read(4096)
            m = date_re.search(head)
            if m:
                dates.add(m.group(1))
        except Exception:
            continue
    return sorted(dates)


# ─── HR cache ─────────────────────────────────────────────────────────────────

def hr_cache_path(date_str: str) -> Path:
    return HR_CACHE_DIR / f"{date_str}.json"


def missing_dates(dates: list[str]) -> list[str]:
    return [d for d in dates if not hr_cache_path(d).exists()]


def fetch_and_cache_hr(client, date_str: str) -> tuple[bool, int]:
    """Fetch HR for one date. Returns (success, sample_count)."""
    try:
        data = client.get_heart_rates(date_str)
    except Exception as e:
        print(f"  {date_str}: fetch failed ({type(e).__name__}: {e})")
        return False, 0

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
    return True, len(samples)


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


def cmd_sync(only_missing: bool = True, throttle: float = 1.0):
    client = get_client(interactive=False)
    all_dates = activity_dates()
    dates = missing_dates(all_dates) if only_missing else all_dates
    if not dates:
        print(f"Nothing to sync - all {len(all_dates)} activity dates already cached.")
        update_status(last_sync=int(time.time()), last_synced=0, total_dates=len(all_dates))
        return

    print(f"Syncing {len(dates)} date{'s' if len(dates) != 1 else ''} "
          f"({len(all_dates)} total activity dates)...")
    synced_ok = 0
    total_samples = 0
    for i, d in enumerate(dates, 1):
        ok, n = fetch_and_cache_hr(client, d)
        if ok:
            synced_ok += 1
            total_samples += n
            tag = f"{n} samples" if n else "no HR data"
            print(f"  [{i}/{len(dates)}] {d}  [OK] {tag}")
        time.sleep(throttle)

    update_status(
        last_sync    = int(time.time()),
        last_synced  = synced_ok,
        total_dates  = len(all_dates),
    )
    print(f"\n[OK] Synced {synced_ok} / {len(dates)} dates ({total_samples:,} HR samples).")


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

    # Cache summary
    all_dates    = activity_dates()
    cached       = [d for d in all_dates if hr_cache_path(d).exists()]
    cached_with  = 0
    cached_empty = 0
    for d in cached:
        try:
            payload = json.loads(hr_cache_path(d).read_text(encoding="utf-8"))
            if payload.get("samples"):
                cached_with += 1
            else:
                cached_empty += 1
        except Exception:
            pass
    print(f"\nCoverage: {len(cached)} / {len(all_dates)} activity dates cached")
    print(f"  with HR data:    {cached_with}")
    print(f"  cached but empty:{cached_empty}")
    print(f"  still to fetch:  {len(all_dates) - len(cached)}")


# ─── Entry ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Garmin HR sync for AlForks.")
    p.add_argument("--login",  action="store_true", help="One-time Garmin login. Reads creds from ~/.alforks/garmin_creds.txt or prompts.")
    p.add_argument("--sync",   action="store_true", help="Pull HR for missing dates.")
    p.add_argument("--resync", action="store_true", help="Re-fetch every activity date (overwrites cache).")
    p.add_argument("--status", action="store_true", help="Show auth + sync status.")
    args = p.parse_args()

    if args.login:    cmd_login()
    elif args.sync:   cmd_sync(only_missing=True)
    elif args.resync: cmd_sync(only_missing=False)
    elif args.status: cmd_status()
    else:             p.print_help()


if __name__ == "__main__":
    main()
