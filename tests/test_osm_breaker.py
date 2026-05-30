"""Tests for the Overpass circuit breaker."""
import time

import osm_breaker


def setup_function(_):
    osm_breaker.reset()


def test_closed_by_default():
    assert osm_breaker.should_skip() is False


def test_threshold_opens_breaker():
    for _ in range(osm_breaker.FAILURE_THRESHOLD):
        osm_breaker.record_failure()
    assert osm_breaker.should_skip() is True


def test_below_threshold_stays_closed():
    for _ in range(osm_breaker.FAILURE_THRESHOLD - 1):
        osm_breaker.record_failure()
    assert osm_breaker.should_skip() is False


def test_success_resets_failure_count():
    for _ in range(osm_breaker.FAILURE_THRESHOLD - 1):
        osm_breaker.record_failure()
    osm_breaker.record_success()
    osm_breaker.record_failure()
    assert osm_breaker.should_skip() is False


def test_breaker_reopens_after_cooldown(monkeypatch):
    base = time.time()
    monkeypatch.setattr(osm_breaker.time, "time", lambda: base)
    for _ in range(osm_breaker.FAILURE_THRESHOLD):
        osm_breaker.record_failure()
    assert osm_breaker.should_skip() is True
    # Advance past the open window — breaker should close again.
    monkeypatch.setattr(osm_breaker.time, "time",
                        lambda: base + osm_breaker.OPEN_SEC + 1)
    assert osm_breaker.should_skip() is False


def test_old_failures_drop_out_of_window(monkeypatch):
    base = time.time()
    monkeypatch.setattr(osm_breaker.time, "time", lambda: base)
    # Two failures, then jump past the window before the third — they
    # should age out, leaving the breaker closed.
    osm_breaker.record_failure()
    osm_breaker.record_failure()
    monkeypatch.setattr(osm_breaker.time, "time",
                        lambda: base + osm_breaker.WINDOW_SEC + 1)
    osm_breaker.record_failure()
    assert osm_breaker.should_skip() is False
