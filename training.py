"""Training-load math for the Training page — pure, unit-tested functions.

The whole page is organized around plain questions ("Should I train today?",
"Am I getting fitter?", ...). Those answers all derive from ONE thing: a daily
training-load number. The hard constraint for this user's data is that **most
rides have no heart rate** (HR started ~2026, and Strava-only rides never have
it), so the load model is HR-OPTIONAL — every ride must count or the trend lies:

  - HR present  -> zone-weighted "effort" load (Edwards-style; recovery <60% HR
                   contributes nothing, higher zones weighted progressively).
  - HR absent   -> a duration x sport-intensity x climb surrogate, CALIBRATED to
                   the rider's own HR-based loads (a single scale factor k, the
                   median HR-load/surrogate ratio over rides that have both) so
                   the two regimes land in the same unit and the curve doesn't
                   kink at the HR boundary.

From a continuous daily-load series we derive (all standard, just hidden behind
plain words in the UI):
  - Fitness  (CTL): EWMA of daily load, 42-day time constant.
  - Fatigue  (ATL): EWMA of daily load, 7-day time constant.
  - Form     (TSB): yesterday's Fitness - yesterday's Fatigue.
  - ACWR        : 7-day average load / 28-day average load (ramp/risk).

To avoid cold-start bias the EWMAs are meant to be run over the rider's FULL
daily history, then sliced to the display window by the caller.
"""
from __future__ import annotations

import math

# Per-bucket weight for HR zone seconds. Buckets are
#   [ <60%, 60-70%, 70-80%, 80-90%, >=90% ] of max HR.
# Recovery (<60%) earns nothing; high intensity is weighted progressively.
ZONE_WEIGHTS = (0.0, 1.0, 2.0, 3.0, 5.0)

# MET value that normalizes the surrogate so a typical hard MTB ride ~ 1.0
# intensity before the climb boost. Keeps surrogate "effort-minutes" comparable
# to the HR load's effort-minutes before calibration trims the rest.
_MET_NORMALIZER = 8.5
# Climb rate (m gained per km) above which the surrogate starts boosting, and
# the metres-per-km that add one full extra unit of intensity.
_CLIMB_KNEE = 30.0
_CLIMB_SPAN = 120.0


def hr_load(zones_sec) -> float:
    """Zone-weighted effort load (in weighted-minutes) from per-zone seconds.

    `zones_sec` is the 5-bucket array [<60,60-70,70-80,80-90,>=90] of seconds.
    Returns 0.0 for empty/missing input."""
    if not zones_sec:
        return 0.0
    return sum((zones_sec[i] / 60.0) * ZONE_WEIGHTS[i]
               for i in range(min(len(ZONE_WEIGHTS), len(zones_sec))))


def surrogate_load(duration_sec: float, gain_m: float, dist_km: float,
                   met: float) -> float:
    """HR-free load estimate: duration x sport-intensity x mild climb boost.

    `met` is the activity type's MET value (sport intensity). Climbing adds up
    to a modest multiplier for steep rides. Units are "effort-minutes" on the
    same scale as hr_load *after* calibration (see calibrate_k)."""
    minutes = max(0.0, duration_sec) / 60.0
    if minutes == 0:
        return 0.0
    intensity = max(0.0, met) / _MET_NORMALIZER
    climb_rate = (gain_m / dist_km) if dist_km and dist_km > 0 else 0.0
    climb_boost = 1.0 + max(0.0, climb_rate - _CLIMB_KNEE) / _CLIMB_SPAN
    return minutes * intensity * climb_boost


def calibrate_k(pairs) -> float:
    """Scale factor that puts surrogate loads into HR-load units.

    `pairs` is an iterable of (hr_load, surrogate_load) for rides that have
    BOTH a real HR load and surrogate inputs. Returns the median ratio
    hr_load/surrogate (robust to outliers), or 1.0 if there's nothing to
    calibrate against."""
    ratios = sorted(h / s for h, s in pairs if s > 0 and h > 0)
    if not ratios:
        return 1.0
    mid = len(ratios) // 2
    if len(ratios) % 2:
        return ratios[mid]
    return (ratios[mid - 1] + ratios[mid]) / 2.0


def ride_load(has_hr: bool, zones_sec, duration_sec: float, gain_m: float,
              dist_km: float, met: float, k: float) -> float:
    """Single ride's load: real HR load when available, else the calibrated
    surrogate. `k` comes from calibrate_k()."""
    if has_hr and zones_sec and any(zones_sec):
        return hr_load(zones_sec)
    return k * surrogate_load(duration_sec, gain_m, dist_km, met)


def ewma(daily_loads, tau_days: float, step_days: float = 1.0,
         seed: float = 0.0) -> list[float]:
    """Exponentially-weighted moving average over a daily load series.

    lambda per step = 1 - exp(-step/tau). Run over the FULL history (seed 0 from
    the rider's first active day) so the displayed window has already converged.
    Returns one smoothed value per input day."""
    lam = 1.0 - math.exp(-step_days / tau_days)
    out = []
    prev = seed
    for v in daily_loads:
        prev = prev + lam * (v - prev)
        out.append(prev)
    return out


# Standard endurance time constants (days).
CTL_TAU = 42.0   # "Fitness"
ATL_TAU = 7.0    # "Fatigue"


def fitness_fatigue_form(daily_loads):
    """Return (fitness[], fatigue[], form[]) aligned to `daily_loads`.

    Fitness = EWMA(42d), Fatigue = EWMA(7d), Form = *yesterday's* (fitness -
    fatigue) so it reads as "freshness coming into today" (lagged by one day,
    the standard convention)."""
    fitness = ewma(daily_loads, CTL_TAU)
    fatigue = ewma(daily_loads, ATL_TAU)
    form = [0.0]
    for i in range(1, len(daily_loads)):
        form.append(fitness[i - 1] - fatigue[i - 1])
    return fitness, fatigue, form


def acwr(daily_loads, idx: int, acute: int = 7, chronic: int = 28):
    """Acute:chronic workload ratio at day `idx` — acute-window average daily
    load over chronic-window average daily load. None until there's a full
    chronic window of history (else the ratio is unstable/misleading)."""
    if idx + 1 < chronic:
        return None
    acute_slice = daily_loads[idx - acute + 1: idx + 1]
    chronic_slice = daily_loads[idx - chronic + 1: idx + 1]
    a = sum(acute_slice) / len(acute_slice)
    c = sum(chronic_slice) / len(chronic_slice)
    if c <= 0:
        return None
    return a / c
