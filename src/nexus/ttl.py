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

    if m := re.fullmatch(r"(\d+)d", low):
        days = int(m.group(1))
        if days == 0:
            raise ValueError(f"TTL must be positive; got {s!r}")
        return days

    if m := re.fullmatch(r"(\d+)w", low):
        weeks = int(m.group(1))
        if weeks == 0:
            raise ValueError(f"TTL must be positive; got {s!r}")
        return weeks * 7

    raise ValueError(
        f"Invalid TTL format {s!r}. Use Nd (days), Nw (weeks), or 'permanent'/'never'."
    )
