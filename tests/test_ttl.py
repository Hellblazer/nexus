"""TTL format parsing: Nd, Nw, permanent/never."""
import pytest

from nexus.ttl import parse_ttl


def test_parse_days() -> None:
    assert parse_ttl("1d") == 1
    assert parse_ttl("30d") == 30
    assert parse_ttl("365d") == 365


def test_parse_weeks() -> None:
    assert parse_ttl("1w") == 7
    assert parse_ttl("4w") == 28
    assert parse_ttl("2w") == 14


def test_parse_permanent() -> None:
    assert parse_ttl("permanent") is None
    assert parse_ttl("never") is None
    assert parse_ttl("PERMANENT") is None
    assert parse_ttl("NEVER") is None


def test_parse_none_returns_none() -> None:
    # None input → permanent (no TTL specified at API level)
    assert parse_ttl(None) is None


def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_ttl("5h")
    with pytest.raises(ValueError):
        parse_ttl("abc")
    with pytest.raises(ValueError):
        parse_ttl("")
    with pytest.raises(ValueError):
        parse_ttl("0d")  # zero days is nonsensical


def test_parse_zero_weeks_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_ttl("0w")


def test_parse_max_boundary_days() -> None:
    """Exactly 36500d (100 years) is accepted."""
    assert parse_ttl("36500d") == 36500


def test_parse_exceeds_max_days_raises() -> None:
    """36501d exceeds 100-year max."""
    with pytest.raises(ValueError, match="too large"):
        parse_ttl("36501d")


def test_parse_max_boundary_weeks() -> None:
    """5214w = 36498d is within the 36500d limit."""
    assert parse_ttl("5214w") == 5214 * 7


def test_parse_exceeds_max_weeks_raises() -> None:
    """5215w = 36505d exceeds the 36500d limit."""
    with pytest.raises(ValueError, match="too large"):
        parse_ttl("5215w")


def test_parse_case_insensitive() -> None:
    assert parse_ttl("30D") == 30
    assert parse_ttl("4W") == 28
