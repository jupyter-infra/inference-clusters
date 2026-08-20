"""Unit tests for the load-test parsing utilities (edge cases for kubelet Pulled-event parsing)."""

from tests.load._loadtest_helpers import parse_pull_duration


def test_parse_pull_duration_minutes_and_seconds() -> None:
    """'3m31.7s' → 211.7 (minutes + fractional seconds)."""
    assert parse_pull_duration("Successfully pulled image ... in 3m31.7s (3m31.7s including waiting)") == 211.7


def test_parse_pull_duration_seconds_only() -> None:
    """'45.3s' → 45.3 (no minutes component)."""
    assert parse_pull_duration('pulled image "x" in 45.3s') == 45.3


def test_parse_pull_duration_whole_minutes() -> None:
    """'2m0s' → 120.0 (whole minutes, zero seconds)."""
    assert parse_pull_duration("in 2m0s") == 120.0


def test_parse_pull_duration_sub_second_milliseconds() -> None:
    """'775ms' must NOT be mis-parsed as 775 seconds — the 'm?s' guards the ms suffix."""
    # kubelet emits e.g. "in 775ms" for tiny images; parser keys on the 's' unit token.
    got = parse_pull_duration("Successfully pulled image ... in 775ms")
    # Either None or a small value, but never 775.0s (the bug this guards against).
    assert got != 775.0


def test_parse_pull_duration_absent_returns_none() -> None:
    """No duration substring → None (not a crash, not 0)."""
    assert parse_pull_duration("Pulling image ...") is None
    assert parse_pull_duration("") is None
