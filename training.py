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


def est_vo2max(max_hr, resting_hr):
    """Rough field VO2max estimate (Uth-Sorensen-Overgaard): 15.3 * HRmax/HRrest.
    A proxy from max + resting HR — not a lab value, but it rises as resting HR
    falls with fitness, and (unlike a running-only Garmin estimate) reflects the
    rider's whole-body aerobic fitness. None if inputs are missing/invalid."""
    if not max_hr or not resting_hr or resting_hr <= 0:
        return None
    return round(15.3 * max_hr / resting_hr, 1)


def polarization(zones_sec):
    """(easy, moderate, hard) fractions from the 5-bucket zone-seconds, using a
    3-zone model: easy <70% HRmax (buckets 0+1), moderate 70-80% (bucket 2),
    hard >=80% (buckets 3+4). Returns None when there's no zoned time."""
    if not zones_sec:
        return None
    z = list(zones_sec) + [0] * (5 - len(zones_sec))
    total = sum(z[:5])
    if total <= 0:
        return None
    return ((z[0] + z[1]) / total, z[2] / total, (z[3] + z[4]) / total)


def monotony(daily_loads):
    """Foster training monotony = mean / population-stddev of daily load over a
    window (include rest days as 0). Higher = more samey = less recovery
    variation. None if <2 days or zero variance (e.g. all rest)."""
    n = len(daily_loads)
    if n < 2:
        return None
    mean = sum(daily_loads) / n
    sd = (sum((x - mean) ** 2 for x in daily_loads) / n) ** 0.5
    if sd == 0:
        return None
    return mean / sd


def trend(values, n: int = 4):
    """Signed relative change between the mean of the last `n` non-None values
    and the `n` before them. None if there isn't enough data. Positive = the
    metric is rising; the caller decides whether rising is good."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return None
    recent = pts[-n:]
    prior = pts[-2 * n:-n] or pts[:-len(recent)] or [pts[0]]
    ra = sum(recent) / len(recent)
    pa = sum(prior) / len(prior)
    if pa == 0:
        return None
    return (ra - pa) / abs(pa)


# ─── Section verdicts (pure; the page renders plain sentences from these) ────

def fitness_verdict(rhr_change, z2_change, fitness_change) -> str:
    """'rising' / 'steady' / 'falling' / 'unknown' for an MTB athlete, from the
    signals that are valid WITHOUT a power meter:
      - resting HR (down = good),
      - Z2 aerobic efficiency, i.e. avg HR at easy effort (down = good),
      - load-derived fitness / chronic load (up = good).
    VO2max is deliberately NOT used: on a running-only Garmin (e.g. FR245) it
    ignores cycling and climbing, so it's unreliable for a mountain biker.
    Each *_change is a signed relative change (from trend()) or None."""
    score = have = 0
    if rhr_change is not None:
        have += 1
        score += 1 if rhr_change < -0.02 else -1 if rhr_change > 0.02 else 0
    if z2_change is not None:
        have += 1
        score += 1 if z2_change < -0.02 else -1 if z2_change > 0.02 else 0
    if fitness_change is not None:
        have += 1
        score += 1 if fitness_change > 0.05 else -1 if fitness_change < -0.05 else 0
    if not have:
        return "unknown"
    return "rising" if score >= 1 else "falling" if score <= -1 else "steady"


def readiness_verdict(bb_end, rhr, rhr_baseline, sleep_hours, form) -> str:
    """'go_hard' / 'steady' / 'easy' / 'unknown' from recovery signals (Body
    Battery, resting HR vs baseline, last night's sleep) plus training Form."""
    have = tired = fresh = 0
    if bb_end is not None:
        have += 1
        if bb_end < 25:
            tired += 1
        elif bb_end >= 60:
            fresh += 1
    if rhr is not None and rhr_baseline:
        have += 1
        if rhr >= rhr_baseline + 5:
            tired += 1
        elif rhr <= rhr_baseline - 1:
            fresh += 1
    if sleep_hours is not None:
        have += 1
        if sleep_hours < 6:
            tired += 1
        elif sleep_hours >= 7.5:
            fresh += 1
    if form is not None:
        have += 1
        if form < -1:
            tired += 1
        elif form > 1:
            fresh += 1
    if have == 0:                      # no signals at all
        return "unknown"
    if tired >= 2 and tired > fresh:
        return "easy"
    if fresh >= 2 and fresh > tired:
        return "go_hard"
    return "steady"                    # have data, but middling


def load_risk_verdict(acwr_val) -> str:
    """'ramping_hard' / 'safe' / 'detraining' / 'unknown' from the ACWR."""
    if acwr_val is None:
        return "unknown"
    if acwr_val > 1.5:
        return "ramping_hard"
    if acwr_val >= 0.8:
        return "safe"
    return "detraining"


def mix_verdict(hard_frac) -> str:
    """'too_hard' / 'balanced' / 'too_easy' / 'unknown' from the hard fraction
    of a 3-zone polarization (aim ~80/20, so >25% hard is skewed hot)."""
    if hard_frac is None:
        return "unknown"
    if hard_frac > 0.25:
        return "too_hard"
    if hard_frac < 0.08:
        return "too_easy"
    return "balanced"


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
