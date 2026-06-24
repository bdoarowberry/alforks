"""Strava activity sync for AlForks GPX viewer.

Setup:
    1. Visit https://www.strava.com/settings/api and create an app:
       - Application Name: AlForks (or anything)
       - Category: Personal
       - Website: http://localhost
       - Authorization Callback Domain: localhost
    2. Save credentials to ~/.alforks/strava_creds.txt:
           client_id=12345
           client_secret=abc123...
    3. python strava_sync.py --login    (one-time browser OAuth)
    4. python strava_sync.py --sync     (pull new activities since last sync)
       python strava_sync.py --resync   (pull EVERY activity, overwrites)
       python strava_sync.py --status
       python strava_sync.py --dedup --dry-run   (report duplicates only)
       python strava_sync.py --dedup             (move dup non-Strava files
                                                  to tracks/_archive_dedup/)

Notes on time format: Strava's API returns true-UTC timestamps, but our
existing TrailForks GPX files store wall-clock-local-as-UTC. The HR-merge
code (in app.py) treats GPX times as local. So we write Strava GPX with
local time tagged "+00:00" to match the existing convention — keeps the
display, HR alignment, and merge logic consistent with the rest of the app.
"""

import argparse
import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.sax.saxutils as sx
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import secure_chmod as _secure_chmod

# ─── Layout ───────────────────────────────────────────────────────────────────
# Project root is the parent of the sync/ folder this script lives in.
_ROOT             = Path(__file__).resolve().parent.parent
GPX_DIR           = _ROOT / "tracks"
ARCHIVE_DIR       = GPX_DIR / "_archive_dedup"

TOKEN_DIR         = Path(os.environ.get("ALFORKS_HOME") or (Path.home() / ".alforks"))
CREDS_FILE        = TOKEN_DIR / "strava_creds.txt"
TOKENS_FILE       = TOKEN_DIR / "strava_tokens.json"
STATUS_FILE       = TOKEN_DIR / "strava_status.json"

OAUTH_PORT        = 8765
REDIRECT_URI      = f"http://localhost:{OAUTH_PORT}/callback"
SCOPE             = "activity:read_all"


# ─── Credentials ──────────────────────────────────────────────────────────────

def read_creds() -> tuple[str, str]:
    if not CREDS_FILE.exists():
        sys.exit("Strava is not configured — no credentials found.\n"
                 "Connect Strava on the Setup page, or create "
                 f"{CREDS_FILE} with two lines:\n"
                 "  client_id=...\n  client_secret=...")
    creds = {}
    for line in CREDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        creds[k.strip().lower()] = v.strip()
    cid, secret = creds.get("client_id", ""), creds.get("client_secret", "")
    if not cid or not secret:
        sys.exit("Strava is not configured — credentials are incomplete "
                 f"(no client_id / client_secret). Reconnect Strava on the "
                 f"Setup page, or fix {CREDS_FILE}.")
    return cid, secret


def load_tokens() -> dict | None:
    if not TOKENS_FILE.exists():
        return None
    try:
        return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_tokens(tok: dict) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _secure_chmod(TOKEN_DIR, 0o700)
    TOKENS_FILE.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    _secure_chmod(TOKENS_FILE, 0o600)


def save_status(s: dict) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _secure_chmod(TOKEN_DIR, 0o700)
    STATUS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def load_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, token: str, params: dict | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ─── OAuth login (one-time) ───────────────────────────────────────────────────

def cmd_login() -> None:
    cid, secret = read_creds()
    auth_url = (
        "https://www.strava.com/oauth/authorize?"
        + urllib.parse.urlencode({
            "client_id":     cid,
            "redirect_uri":  REDIRECT_URI,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope":         SCOPE,
        })
    )

    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if "code" in params:
                captured["code"]  = params["code"][0]
                captured["scope"] = params.get("scope", [""])[0]
                msg = "Strava authorization received. You can close this tab."
            else:
                captured["error"] = params.get("error", ["unknown"])[0]
                msg = f"Strava authorization failed: {captured['error']}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode("utf-8"))

    print(f"Opening browser:\n  {auth_url}\n")
    print(f"Waiting for redirect to {REDIRECT_URI} ...")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    with socketserver.TCPServer(("localhost", OAUTH_PORT), Handler) as srv:
        srv.timeout = 120
        # handle_request() blocks until one request OR timeout
        srv.handle_request()

    if "code" not in captured:
        sys.exit(f"Login failed: {captured.get('error', 'no code received')}")

    print("Exchanging code for tokens...")
    tok = _post("https://www.strava.com/oauth/token", {
        "client_id":     cid,
        "client_secret": secret,
        "code":          captured["code"],
        "grant_type":    "authorization_code",
    })
    if "access_token" not in tok:
        sys.exit(f"Token exchange failed: {tok}")
    save_tokens(tok)
    print(f"\n[OK] Logged in as: {tok.get('athlete', {}).get('firstname', '')} "
          f"{tok.get('athlete', {}).get('lastname', '')}")
    print(f"  Tokens saved to: {TOKENS_FILE}")


def get_access_token() -> str:
    """Return a valid access token, refreshing it if expired."""
    tok = load_tokens()
    if not tok:
        sys.exit("Not logged in. Run: python strava_sync.py --login")
    expires = int(tok.get("expires_at", 0))
    if expires - 60 > time.time():
        return tok["access_token"]
    # Refresh
    cid, secret = read_creds()
    new = _post("https://www.strava.com/oauth/token", {
        "client_id":     cid,
        "client_secret": secret,
        "grant_type":    "refresh_token",
        "refresh_token": tok["refresh_token"],
    })
    if "access_token" not in new:
        # Refresh token revoked/expired — the saved login is no longer valid.
        sys.exit("Strava login expired — please reconnect on the Setup page. "
                 f"(token refresh failed: {new})")
    # Strava returns a new refresh_token too — replace both
    tok.update(new)
    save_tokens(tok)
    return tok["access_token"]


# ─── Activity discovery ───────────────────────────────────────────────────────

def list_activities(token: str, after_epoch: int | None = None) -> list[dict]:
    """Fetch all activities (paginated). `after_epoch` filters to activities
    whose start_date is after that UNIX timestamp."""
    out: list[dict] = []
    page = 1
    while True:
        params = {"per_page": 200, "page": page}
        if after_epoch:
            params["after"] = after_epoch
        try:
            batch = _get("https://www.strava.com/api/v3/athlete/activities", token, params)
        except urllib.error.HTTPError as e:
            print(f"  list activities page {page} failed: {e}")
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return out


def fetch_streams(token: str, activity_id: int) -> dict | None:
    try:
        return _get(
            f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
            token,
            {"keys": "latlng,altitude,time", "key_by_type": "true"},
        )
    except urllib.error.HTTPError as e:
        print(f"  streams {activity_id} failed: {e}")
        return None


# ─── GPX writing ──────────────────────────────────────────────────────────────

def gpx_path_for(activity_id: int) -> Path:
    return GPX_DIR / f"strava_{activity_id}.gpx"


def archived_path_for(activity_id: int) -> Path:
    """Where this activity would live if the user had archived it. The sync
    skips re-downloading anything found here so a Review → Delete on a
    duplicate doesn't get undone the next time Strava sync runs."""
    return ARCHIVE_DIR / f"strava_{activity_id}.gpx"


# Strava's sport_type taxonomy → AlForks meta.type. Anything not listed
# (Ride, EBikeRide, Run, …) stays untagged so the user can decide; the
# obvious mappings ride and run aren't necessarily MTB / hike.
_STRAVA_SPORT_TO_TYPE = {
    "AlpineSki":          "ski",
    "BackcountrySki":     "ski",
    "NordicSki":          "ski",
    "Snowboard":          "snowboard",
    "MountainBikeRide":   "mtb",
    "EMountainBikeRide":  "mtb",
    "GravelRide":         "mtb",
    "Hike":               "hike",
    "Walk":               "hike",
    "TrailRun":           "hike",
}

METADATA_FILE = _ROOT / "instance" / "metadata.json"


def _apply_sport_type_tag(filename: str, sport_type: str | None) -> str | None:
    """Set metadata[filename].type based on Strava sport_type if a mapping
    exists and the file isn't already tagged. Returns the applied tag, or
    None if nothing was done. Idempotent — never overwrites a manual tag.

    Note: the Flask app caches metadata.json in memory at process start, so
    tags written here only become visible after the next Flask restart (or
    after the user edits any other field, which triggers a reload).
    """
    tag = _STRAVA_SPORT_TO_TYPE.get(sport_type or "")
    if not tag:
        return None
    try:
        meta = json.loads(METADATA_FILE.read_text(encoding="utf-8")) if METADATA_FILE.exists() else {}
    except Exception:
        return None
    entry = meta.get(filename) or {}
    if entry.get("type"):
        return None  # already tagged — respect manual edits
    entry["type"] = tag
    meta[filename] = entry
    tmp = METADATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(METADATA_FILE)
    return tag


def write_gpx(activity: dict, streams: dict) -> bool:
    """Build a minimal GPX file from an activity's streams. Returns True if
    written, False if the activity has no usable GPS data."""
    latlng = (streams.get("latlng") or {}).get("data") or []
    if not latlng:
        return False
    altitude = (streams.get("altitude") or {}).get("data") or [None] * len(latlng)
    time_off = (streams.get("time")     or {}).get("data") or list(range(len(latlng)))

    # Use start_date_local as wall-clock local but tag as +00:00 to match
    # the existing TrailForks-style files. start_date_local is ISO with a Z
    # suffix; strip the Z, treat as naive.
    start_local = (activity.get("start_date_local") or "").rstrip("Z")
    try:
        anchor = datetime.fromisoformat(start_local)  # naive, represents local wall clock
    except Exception:
        return False

    name = activity.get("name") or f"Strava activity {activity['id']}"
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="AlForks Strava sync" xmlns="http://www.topografix.com/GPX/1/1">',
             '  <trk>',
             f'    <name>{sx.escape(name)}</name>',
             '    <trkseg>']
    for i, (lat, lon) in enumerate(latlng):
        ele = altitude[i] if i < len(altitude) else None
        ts  = anchor + timedelta(seconds=int(time_off[i] if i < len(time_off) else 0))
        # Wall-clock local tagged as +00:00 (matches TrailForks convention)
        ts_str = ts.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        parts.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">')
        if ele is not None:
            parts.append(f'        <ele>{ele:.1f}</ele>')
        parts.append(f'        <time>{ts_str}</time>')
        parts.append('      </trkpt>')
    parts.append('    </trkseg>')
    parts.append('  </trk>')
    parts.append('</gpx>')

    GPX_DIR.mkdir(parents=True, exist_ok=True)
    out = gpx_path_for(activity["id"])
    tmp = out.with_suffix(".gpx.tmp")
    tmp.write_text("\n".join(parts), encoding="utf-8")
    tmp.replace(out)
    return True


# ─── Sync ─────────────────────────────────────────────────────────────────────

def _advance_newest_epoch(newest_epoch: int, a: dict) -> int:
    """Fold an activity's start time into the running newest-epoch high-water
    mark. Best-effort: a missing or garbled start_date leaves it unchanged."""
    try:
        # Strava's start_date is true-UTC with a trailing "Z"; keep the tz so
        # .timestamp() yields a correct UTC epoch instead of assuming local time
        # (which would shift the incremental-sync "after" boundary and skip rides).
        start_dt = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).timestamp()
        return max(newest_epoch, int(start_dt))
    except Exception:
        return newest_epoch


def _emit_progress(phase: str, done: int, total: int) -> None:
    """Machine-readable progress line the app's sync runner parses to drive the
    per-phase status UI. Plain stdout, so it survives the subprocess boundary;
    ignored by anything that doesn't understand it."""
    print(f"@@PROGRESS {json.dumps({'phase': phase, 'done': done, 'total': total})}",
          flush=True)


def _after_epoch(s: str) -> int:
    """Parse 'YYYY-MM-DD' to a local-midnight UNIX epoch, or 0 if blank/invalid."""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").timestamp())
    except ValueError:
        return 0


def cmd_sync(only_new: bool = True, after_floor: int = 0) -> None:
    token  = get_access_token()
    status = load_status()
    # Incremental syncs floor at the newest cached ride; `after_floor` (the
    # user's chosen start date) additionally bounds the *first* sync so a fresh
    # copy doesn't pull a whole multi-year history at once.
    after  = max(int(status.get("last_activity_epoch", 0)) if only_new else 0, after_floor)
    if after:
        print(f"Pulling activities after {datetime.fromtimestamp(after).isoformat()}")
    else:
        print("Pulling ALL activities...")

    acts = list_activities(token, after_epoch=after if after else None)
    print(f"Found {len(acts)} activities to consider.")
    _emit_progress("download", 0, len(acts))

    GPX_DIR.mkdir(parents=True, exist_ok=True)
    written = skipped = empty = 0
    # Seed the watermark at `after` (which already folds in the start-date floor)
    # so a sync that finds zero new rides still advances last_activity_epoch to the
    # floor — we never re-scan pre-floor history on the next run.
    newest_epoch = after

    for i, a in enumerate(acts):
        _emit_progress("download", i, len(acts))
        aid = a["id"]
        out = gpx_path_for(aid)
        archived = archived_path_for(aid)
        # `only_new` means "skip activities we've already pulled". Both the
        # active tracks/ folder and the _archive_dedup/ tombstone count —
        # otherwise activities the user explicitly deleted in /review get
        # silently re-downloaded on the next sync.
        if (out.exists() or archived.exists()) and only_new:
            skipped += 1
            newest_epoch = _advance_newest_epoch(newest_epoch, a)
            continue
        # Skip non-GPS activities (manual, virtual without GPS, etc.)
        if not a.get("start_latlng"):
            empty += 1
            continue
        time.sleep(0.7)  # gentle rate-limit
        streams = fetch_streams(token, aid)
        if not streams:
            empty += 1
            continue
        if write_gpx(a, streams):
            applied_tag = _apply_sport_type_tag(out.name, a.get("sport_type") or a.get("type"))
            tag_msg = f"  [tag: {applied_tag}]" if applied_tag else ""
            print(f"  + {a.get('start_date_local','?')[:16]}  {a.get('name','')[:60]}  -> {out.name}{tag_msg}")
            written += 1
            newest_epoch = _advance_newest_epoch(newest_epoch, a)
        else:
            empty += 1

    _emit_progress("download", len(acts), len(acts))
    save_status({
        "last_sync":           int(time.time()),
        "last_activity_epoch": int(newest_epoch),
        "total_files":         len(list(GPX_DIR.glob("strava_*.gpx"))),
    })
    print(f"\nDone. Wrote {written}, skipped {skipped} (already cached), {empty} had no GPS data.")


# ─── Dedup ────────────────────────────────────────────────────────────────────

def _first_time_of(path: Path) -> tuple[str, datetime] | None:
    """Return (date_str, first-trkpt-as-datetime) by reading just the head of
    the GPX file. Naive datetime; we only compare wall-clock time within a day.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:8192]
    except Exception:
        return None
    m = re.search(r"<time>(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})", head)
    if not m:
        return None
    date_str = m.group(1)
    dt = datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]), int(m.group(1)[8:10]),
                  int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return date_str, dt


def cmd_dedup(dry_run: bool = True) -> None:
    """For each strava_*.gpx, find non-Strava files with the same date whose
    first timestamp is within ±10 minutes. Move the non-Strava ones to
    tracks/_archive_dedup/ (or report only when --dry-run).
    """
    strava_files = sorted(GPX_DIR.glob("strava_*.gpx"))
    other_files  = [p for p in GPX_DIR.glob("*.gpx") if not p.name.startswith("strava_")]
    if not strava_files:
        print("No Strava-sourced files yet — run --sync first.")
        return

    other_index: dict[str, list[tuple[Path, datetime]]] = {}
    for p in other_files:
        info = _first_time_of(p)
        if info:
            other_index.setdefault(info[0], []).append((p, info[1]))

    pairs: list[tuple[Path, Path, int]] = []  # (strava, dup, abs_diff_minutes)
    for sp in strava_files:
        info = _first_time_of(sp)
        if not info: continue
        date_str, sdt = info
        for other_path, odt in other_index.get(date_str, []):
            diff_min = abs((sdt - odt).total_seconds()) / 60
            if diff_min <= 10:
                pairs.append((sp, other_path, int(round(diff_min))))

    if not pairs:
        print("No duplicates detected.")
        return

    print(f"Found {len(pairs)} duplicate pair(s):")
    for sp, dp, diff in pairs:
        print(f"  Strava  {sp.name}  ↔  duplicate  {dp.name}   (Δ {diff} min)")

    if dry_run:
        print("\n--dry-run set, no changes made. Re-run without --dry-run to move duplicates.")
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for _, dp, _ in pairs:
        dest = ARCHIVE_DIR / dp.name
        if dest.exists():
            dest = ARCHIVE_DIR / f"{dp.stem}_{int(time.time())}{dp.suffix}"
        try:
            shutil.move(str(dp), str(dest))
            moved += 1
        except Exception as e:
            print(f"  failed to move {dp.name}: {e}")
    print(f"\nMoved {moved} duplicate(s) to {ARCHIVE_DIR}")


# ─── Status ───────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    if not TOKENS_FILE.exists():
        print(f"Not logged in. Run: python strava_sync.py --login")
        return
    s = load_status()
    files = list(GPX_DIR.glob("strava_*.gpx"))
    print(f"Strava files in tracks/: {len(files)}")
    if s.get("last_sync"):
        print(f"Last sync:   {datetime.fromtimestamp(s['last_sync']).isoformat()}")
    if s.get("last_activity_epoch"):
        print(f"Newest activity: {datetime.fromtimestamp(s['last_activity_epoch']).isoformat()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login",  action="store_true")
    ap.add_argument("--sync",   action="store_true")
    ap.add_argument("--resync", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dedup",  action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Used with --dedup to preview matches")
    ap.add_argument("--after", default="",
                    help="Earliest ride date to import, YYYY-MM-DD (used with --sync/--resync)")
    args = ap.parse_args()

    if args.login:    cmd_login()
    elif args.resync: cmd_sync(only_new=False, after_floor=_after_epoch(args.after))
    elif args.sync:   cmd_sync(only_new=True, after_floor=_after_epoch(args.after))
    elif args.dedup:  cmd_dedup(dry_run=args.dry_run)
    elif args.status: cmd_status()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
