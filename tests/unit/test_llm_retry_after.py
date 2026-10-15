"""Unit tests — Retry-After parsing for the in-flight-budget retry (T114, FR-019).

The clock is fixed by replacing ``veritas.llm.client.datetime`` with a subclass
whose ``now()`` returns FIXED_NOW, so every expected delay is exact.
"""

from datetime import datetime, timezone

import pytest

import veritas.llm.client as client_module
from veritas.llm.client import (
    MAX_IN_FLIGHT_RETRY_DELAY,
    _retry_after_seconds,
    in_flight_retry_delay,
)

# Monday 12 October 2026, 12:00:00 UTC.
FIXED_NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(client_module, "datetime", _FixedDatetime)


def _in_flight_402(retry_after: str) -> BaseException:
    """An OpenRouter in-flight-budget 402 carrying the given Retry-After value."""

    class InFlight402(BaseException):
        status_code = 402
        body = {
            "error": {
                "message": "credits",
                "metadata": {"reason": "in_flight_budget_exhausted"},
            }
        }
        headers = {"retry-after": retry_after}

    return InFlight402()


def test_http_date_30_seconds_ahead_gives_30_seconds():
    exc = _in_flight_402("Mon, 12 Oct 2026 12:00:30 GMT")
    assert _retry_after_seconds(exc) == 30.0
    assert in_flight_retry_delay(exc) == 30.0


def test_http_date_far_ahead_is_capped_by_in_flight_retry_delay():
    exc = _in_flight_402("Tue, 13 Oct 2026 12:00:00 GMT")  # one day ahead
    # The parser reports the real wait; the 120s cap is applied by its caller.
    assert _retry_after_seconds(exc) == 86400.0
    assert in_flight_retry_delay(exc) == MAX_IN_FLIGHT_RETRY_DELAY == 120.0


def test_http_date_in_the_past_gives_zero_not_a_negative_delay():
    exc = _in_flight_402("Mon, 12 Oct 2026 11:59:00 GMT")  # one minute ago
    assert _retry_after_seconds(exc) == -60.0
    assert in_flight_retry_delay(exc) == 0.0


def test_unparseable_value_gives_zero_and_no_exception():
    exc = _in_flight_402("soon, probably")
    assert _retry_after_seconds(exc) == 0.0
    assert in_flight_retry_delay(exc) == 0.0


def test_seconds_form_is_unchanged():
    assert _retry_after_seconds(_in_flight_402("5")) == 5.0
    assert in_flight_retry_delay(_in_flight_402("5")) == 5.0
    assert _retry_after_seconds(_in_flight_402("1.5")) == 1.5
    assert in_flight_retry_delay(_in_flight_402("500")) == 120.0


def test_minus_0000_date_gives_the_same_delay_as_gmt():
    # RFC 2822 "-0000" means UTC with the local zone unknown; it parses to a
    # datetime without tzinfo, which must be read as UTC, not fall back to 0.
    gmt = _in_flight_402("Mon, 12 Oct 2026 12:00:30 GMT")
    unknown_zone = _in_flight_402("Mon, 12 Oct 2026 12:00:30 -0000")
    assert _retry_after_seconds(unknown_zone) == _retry_after_seconds(gmt) == 30.0
    assert in_flight_retry_delay(unknown_zone) == in_flight_retry_delay(gmt) == 30.0
