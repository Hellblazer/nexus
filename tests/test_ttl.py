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
