"""
Guard rails for app/utils/equity_price_feed.py.

These exist because a NameError on get_prices() once reached production: the
staleness guard referenced a helper that did not exist, so every call raised and
the whole WebSocket price feed silently degraded to REST. Nothing was calling
these functions in a test, so nothing caught it.

The feed is deliberately import-safe with no WebSocket and no Flask app, so it
can be exercised directly.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FEED_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "equity_price_feed.py"


def _load():
    spec = importlib.util.spec_from_file_location("equity_price_feed_under_test", FEED_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["equity_price_feed_under_test"] = module
    spec.loader.exec_module(module)
    return module


feed_module = _load()
KEYS = [("RELIANCE", "NSE"), ("TCS", "NSE")]


def _fresh_feed():
    return feed_module.EquityPriceFeed() if hasattr(feed_module, "EquityPriceFeed") else feed_module.equity_price_feed


class TestCallableWithoutAWebSocket:
    """Every public entry point must return, not raise, when no feed is up."""

    def test_get_prices_does_not_raise(self):
        assert feed_module.equity_price_feed.get_prices(KEYS) == {}

    def test_get_prices_on_empty_input(self):
        assert feed_module.equity_price_feed.get_prices([]) == {}

    def test_ensure_subscribed_does_not_raise(self):
        result = feed_module.equity_price_feed.ensure_subscribed(KEYS)
        assert isinstance(result, dict)

    def test_prime_does_not_raise(self):
        assert isinstance(feed_module.equity_price_feed.prime(KEYS), dict)

    def test_status_does_not_raise(self):
        status = feed_module.equity_price_feed.status()
        assert isinstance(status, dict)
        assert "subscribed" in status

    def test_release_does_not_raise(self):
        assert isinstance(feed_module.equity_price_feed.release(KEYS), int)


class TestStalenessGuard:
    """
    The guard that caused the outage. A price past MAX_PRICE_AGE_SECONDS must be
    reported as absent so the caller's REST backstop refreshes it, and the
    comparison must be timezone aware in both directions.
    """

    def test_max_price_age_is_defined_and_sane(self):
        age = feed_module.MAX_PRICE_AGE_SECONDS
        assert age > 30, "must exceed the 30 second screen poll or a healthy feed is second-guessed"
        assert age < 600, "must be low enough that a dead subscription cannot look live"

    def test_a_fresh_pushed_price_is_returned(self):
        f = feed_module.equity_price_feed
        key = ("FRESHSYM", "NSE")
        with f._lock:
            f._prices[key] = 101.5
            f._price_times[key] = datetime.now(timezone.utc)
        try:
            assert f.get_prices([key]).get(key) == pytest.approx(101.5)
        finally:
            with f._lock:
                f._prices.pop(key, None)
                f._price_times.pop(key, None)

    def test_an_aged_price_is_treated_as_absent(self):
        f = feed_module.equity_price_feed
        key = ("STALESYM", "NSE")
        old = datetime.now(timezone.utc) - timedelta(seconds=feed_module.MAX_PRICE_AGE_SECONDS + 30)
        with f._lock:
            f._prices[key] = 101.5
            f._price_times[key] = old
        try:
            # Absent, not zero and not an exception, so the caller falls back.
            assert key not in f.get_prices([key])
        finally:
            with f._lock:
                f._prices.pop(key, None)
                f._price_times.pop(key, None)

    def test_stored_tick_times_are_timezone_aware(self):
        # A naive datetime here would make the cutoff comparison raise TypeError.
        f = feed_module.equity_price_feed
        key = ("TZSYM", "NSE")
        with f._lock:
            f._prices[key] = 10.0
            f._price_times[key] = datetime.now(timezone.utc)
            stored = f._price_times[key]
        try:
            assert stored.tzinfo is not None
            f.get_prices([key])
        finally:
            with f._lock:
                f._prices.pop(key, None)
                f._price_times.pop(key, None)
