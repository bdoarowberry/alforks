"""Shared circuit breaker for Overpass API fetches.

Three independent call paths hit Overpass (lifts in app.py; trails and
roads in trail_match.py). When Overpass is slow or unreachable, the
default urlopen timeout is OVERPASS_TIMEOUT_SEC + 20 (= 80 s), and each
caller burns that wait independently. During a prewarm — when 6 worker
threads attempt to fetch in parallel for nearby bboxes — this stacks
into minutes of dead time before they all fall through to empty results.

This module records per-endpoint failures across all three fetchers
sharing the same Overpass host. After N consecutive failures within a
short window, the breaker opens for OPEN_SEC; subsequent calls return
True from `should_skip()` and the caller serves stale cache (or empty)
without touching the network.

Stale-cache fallback is the orthogonal half: on any failure (timeout,
network error, breaker-open), the caller reads its on-disk cache file
ignoring TTL. Cache files past TTL are still semantically correct OSM
data; they're just stale. Better to serve last-known-good than nothing.
"""
from __future__ import annotations

import threading
import time

# Tuned for a single-user app on a residential connection. If Overpass
# is unreachable, the first 3 calls in any 5-minute window pay the full
# urlopen timeout; the breaker then suppresses further attempts for 60 s.
FAILURE_THRESHOLD = 3
WINDOW_SEC = 300
OPEN_SEC = 60

_failures: list[float] = []
_open_until: float = 0.0
_lock = threading.Lock()


def should_skip() -> bool:
    """True if the breaker is currently open — caller should not hit the network."""
    with _lock:
        return time.time() < _open_until


def record_failure() -> None:
    """Increment the failure counter; open the breaker if the threshold is reached."""
    global _open_until
    now = time.time()
    with _lock:
        cutoff = now - WINDOW_SEC
        _failures[:] = [t for t in _failures if t >= cutoff]
        _failures.append(now)
        if len(_failures) >= FAILURE_THRESHOLD:
            _open_until = now + OPEN_SEC
            _failures.clear()


def record_success() -> None:
    """Clear all failure state — Overpass is reachable again."""
    global _open_until
    with _lock:
        _failures.clear()
        _open_until = 0.0


def reset() -> None:
    """Force breaker back to closed state with no failure history. Intended
    for tests; production code should use record_success() instead."""
    global _open_until
    with _lock:
        _failures.clear()
        _open_until = 0.0
