# SPDX-License-Identifier: AGPL-3.0-or-later
import re


def parse_ttl(s: str | None) -> int | None:
    """Parse a TTL string to an integer number of days, or None for permanent.

    Accepts:
      - ``Nd``        — N days  (e.g. ``30d``)
      - ``Nw``        — N weeks (e.g. ``4w``)
      - ``permanent`` or ``never`` — no expiry (returns None)
      - ``None``      — treated as permanent (returns None)

    Raises ValueError for unrecognised or zero-valued formats.
    """
    if s is None:
        return None
    low = s.lower()
    if low in ("permanent", "never"):
        return None

    _MAX_TTL_DAYS = 36500  # 100 years; beyond this datetime arithmetic overflows

    if m := re.fullmatch(r"(\d+)d", low):
        days = int(m.group(1))
        if days == 0:
            raise ValueError(f"TTL must be positive; got {s!r}")
        if days > _MAX_TTL_DAYS:
            raise ValueError(f"TTL too large: {days}d exceeds maximum {_MAX_TTL_DAYS}d (100 years)")
        return days

    if m := re.fullmatch(r"(\d+)w", low):
        weeks = int(m.group(1))
        if weeks == 0:
            raise ValueError(f"TTL must be positive; got {s!r}")
        days = weeks * 7
        if days > _MAX_TTL_DAYS:
            raise ValueError(f"TTL too large: {weeks}w exceeds maximum {_MAX_TTL_DAYS}d (100 years)")
        return days

    raise ValueError(
        f"Invalid TTL format {s!r}. Use Nd (days), Nw (weeks), or 'permanent'/'never'."
    )
