"""Elevation-gain smoothing harness.

Background: AlForks currently computes `elev_gain_m` as the straight sum of
positive consecutive ele_deltas (the `sm_delta > 0` branch of the per-point
ride-stats loop in app.py). On rides where the source GPX
carries raw barometric data this over-reads by 4-72% vs. Trailforks'
displayed gain. This script loads a calibration set of real rides (with
known TF / Strava reference numbers) and reports what each candidate
smoothing strategy would compute.

Ground truth is Trailforks per `memory/recording_pipeline.md` — Strava
mirrors TF after auto-push, so they're not independent estimates. Strava
is shown for context but not used as truth.

Usage:
    python scripts/elev_smoothing.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Callable, NamedTuple

# Use the project's gpxpy + parser path so we read the same data the live
# app reads. Cheaper than reinventing a GPX reader.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gpxpy  # type: ignore


class Ride(NamedTuple):
    filename: str
    tf_m: int           # Trailforks displayed gain — treated as ground truth
    st_m: int           # Strava displayed gain — context only
    label: str          # human description
    dupe_group: str     # rides with the same dupe_group are the same physical
                        # ride from different export sources; smoothing should
                        # collapse their AlForks readings toward each other.


# All values from user-supplied calibration data (see conversation
# 2026-05-08 → 2026-05-10). Bike-park / ski days deliberately excluded
# — TF doesn't subtract lift uplift the way AlForks does, so the
# quantities aren't comparable on assisted rides.
CALIBRATION: list[Ride] = [
    Ride('strava_18457321567.gpx',   79,   80, '2026-05-10 short flat',         'solo-a'),
    Ride('strava_18352063219.gpx',  609,  609, '2026-05-02 medium clean',       'solo-b'),
    Ride('strava_18443951562.gpx',  767,  764, '2026-05-09 medium',             'solo-c'),
    Ride('ridelog_2024-04-27.gpx',  449,  488, '2024-04-27 dupe (TF-export)',   'dupe-apr'),
    Ride('strava_11400397330.gpx',  449,  488, '2024-04-27 dupe (ST-export)',   'dupe-apr'),
    Ride('ridelog_2024-10-06.gpx', 1434, 1357, '2024-10-06 dupe (TF-export)',   'dupe-oct'),
    Ride('strava_12593909783.gpx', 1434, 1357, '2024-10-06 dupe (ST-export)',   'dupe-oct'),
]


def load_elevations(filename: str) -> list[float]:
    """Return the elevation series in metres, in track order, skipping
    None values. Matches the read path the live parser uses (gpxpy) so
    we're tuning against exactly the same input the app sees."""
    path = ROOT / 'tracks' / filename
    with open(path, encoding='utf-8') as f:
        gpx = gpxpy.parse(f)
    eles: list[float] = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            for pt in seg.points:
                if pt.elevation is not None:
                    eles.append(float(pt.elevation))
    return eles


# ── Candidate strategies ────────────────────────────────────────────────────
# Each strategy is a (name, fn) where fn takes an elevation series and
# returns elev_gain_m. Compare against ride.tf_m (ground truth).

def s_raw(eles: list[float]) -> float:
    """Current AlForks behaviour: sum of positive ele_deltas, no smoothing."""
    g = 0.0
    for i in range(1, len(eles)):
        d = eles[i] - eles[i-1]
        if d > 0: g += d
    return g


def _moving_avg(eles: list[float], k: int) -> list[float]:
    """Simple centred moving average of window size k (odd). Edges fall
    back to whatever's available in the partial window."""
    n = len(eles)
    half = k // 2
    out = [0.0] * n
    for i in range(n):
        lo = max(0, i - half); hi = min(n, i + half + 1)
        out[i] = sum(eles[lo:hi]) / (hi - lo)
    return out


def s_ma(k: int) -> Callable[[list[float]], float]:
    """Moving-average smoothing then sum positive deltas. Reduces high-
    frequency sensor jitter; preserves real climbs because they span many
    samples."""
    def fn(eles):
        sm = _moving_avg(eles, k)
        g = 0.0
        for i in range(1, len(sm)):
            d = sm[i] - sm[i-1]
            if d > 0: g += d
        return g
    fn.__name__ = f'ma{k}'
    return fn


def s_min_delta(threshold_m: float) -> Callable[[list[float]], float]:
    """Hysteresis on cumulative elevation: commit a climb only when the
    altitude has risen by `threshold_m` from the running anchor before
    reversing by `threshold_m` from the running peak. Standard approach
    for GPS-derived gain on hiking watches — drops sensor wobble below
    the threshold without losing real climbs."""
    def fn(eles):
        if not eles: return 0.0
        gain = 0.0
        anchor = eles[0]      # last confirmed reversal point
        running = eles[0]     # running max (climbing) or min (descending)
        direction = 0         # +1 climbing, -1 descending, 0 unknown
        for v in eles[1:]:
            if direction == 1:
                # Tracking running max while in a climb
                if v > running:
                    running = v
                elif running - v >= threshold_m:
                    # Reversed by threshold from the peak — lock in gain
                    gain += running - anchor
                    anchor = running; running = v; direction = -1
            elif direction == -1:
                # Tracking running min while in a descent
                if v < running:
                    running = v
                elif v - running >= threshold_m:
                    # Reversed by threshold from the trough — start a new climb
                    anchor = running; running = v; direction = 1
            else:
                # Direction unknown — wait for the first confirmed move
                if v >= anchor + threshold_m:
                    direction = 1; running = v
                elif v <= anchor - threshold_m:
                    direction = -1; running = v
        # Trailing climb beyond the last confirmed reversal
        if direction == 1 and running > anchor:
            gain += running - anchor
        return gain
    fn.__name__ = f'thr{threshold_m}'
    return fn


def s_ma_min_delta(k: int, threshold_m: float) -> Callable[[list[float]], float]:
    """Apply moving-average smoothing, then the hysteresis threshold. The
    two filters target different noise: MA handles fast jitter, threshold
    handles drift-driven slow wobble."""
    thr = s_min_delta(threshold_m)
    def fn(eles):
        sm = _moving_avg(eles, k)
        return thr(sm)
    fn.__name__ = f'ma{k}+thr{threshold_m}'
    return fn


STRATEGIES: list[tuple[str, Callable[[list[float]], float]]] = [
    ('raw (current)',  s_raw),
    ('ma10',           s_ma(10)),
    ('ma15',           s_ma(15)),
    ('ma20',           s_ma(20)),
    ('thr3m',          s_min_delta(3.0)),
    ('thr5m',          s_min_delta(5.0)),
    ('ma10+thr2m',     s_ma_min_delta(10, 2.0)),
    ('ma10+thr3m',     s_ma_min_delta(10, 3.0)),
    ('ma15+thr2m',     s_ma_min_delta(15, 2.0)),
    ('ma15+thr3m',     s_ma_min_delta(15, 3.0)),
    ('ma20+thr2m',     s_ma_min_delta(20, 2.0)),
    ('ma20+thr3m',     s_ma_min_delta(20, 3.0)),
]


# ── Reporting ───────────────────────────────────────────────────────────────

def main() -> None:
    # Cache loaded elevations so dupe-pair files only get parsed once
    # apiece (they're different files but the loop is cheap anyway).
    eles_by_file: dict[str, list[float]] = {}
    for r in CALIBRATION:
        eles_by_file[r.filename] = load_elevations(r.filename)

    # ── Per-ride table ───────────────────────────────────────────────
    header_strategies = [s[0] for s in STRATEGIES]
    col_w = 12
    label_w = 38

    print()
    print(f'{"ride":<{label_w}}{"tf":>6}{"st":>6}  |  '
          + ''.join(f'{n:>{col_w}}' for n in header_strategies))
    print('-' * (label_w + 14 + len(header_strategies) * col_w))
    results: dict[str, dict[str, float]] = {}
    for r in CALIBRATION:
        eles = eles_by_file[r.filename]
        row = {}
        for name, fn in STRATEGIES:
            row[name] = fn(eles)
        results[r.filename] = row
        cells = ''.join(f'{round(row[n]):>{col_w}}' for n in header_strategies)
        print(f'{r.label:<{label_w}}{r.tf_m:>6}{r.st_m:>6}  |  {cells}')

    # ── Goodness summary per strategy ────────────────────────────────
    # Two metrics:
    #   1. Mean absolute error vs. TF across all rides (lower = better)
    #   2. Within-dupe-pair spread (lower = better; tests convergence)
    print()
    print(f'{"strategy":<{label_w}}{"MAE_vs_TF":>12}{"dupe_spread":>14}{"max_err":>10}')
    print('-' * (label_w + 12 + 14 + 10))

    dupe_groups: dict[str, list[Ride]] = {}
    for r in CALIBRATION:
        dupe_groups.setdefault(r.dupe_group, []).append(r)

    for name, _ in STRATEGIES:
        errs = []
        max_err = 0.0
        for r in CALIBRATION:
            v = results[r.filename][name]
            e = abs(v - r.tf_m)
            errs.append(e)
            if e > max_err: max_err = e
        mae = sum(errs) / len(errs) if errs else 0.0

        # Dupe spread: for each dupe_group with 2+ rides, max - min of
        # this strategy's values. Solo groups contribute 0.
        spread_sum = 0.0
        spread_n = 0
        for g, members in dupe_groups.items():
            if len(members) < 2: continue
            vals = [results[m.filename][name] for m in members]
            spread_sum += max(vals) - min(vals)
            spread_n += 1
        avg_spread = (spread_sum / spread_n) if spread_n else 0.0

        print(f'{name:<{label_w}}{mae:>12.1f}{avg_spread:>14.1f}{max_err:>10.1f}')


if __name__ == '__main__':
    main()
