# SPDX-License-Identifier: AGPL-3.0-or-later
import re

#: 100 years: a policy cap, not an overflow point (a date plus a TTL overflows near 2.9M days, and
#: ``timedelta`` itself holds 999,999,999). The one cap every TTL writer applies: ``nx store put --ttl``,
#: ``put_note``, ``write_note`` and ``T3Database.put`` refuse above it, and a bundle import clamps to it.
MAX_TTL_DAYS = 36500


def refuse_ttl_over_cap(ttl_days: int | None) -> None:
    """Raise ``ValueError`` for a ``ttl_days`` above :data:`MAX_TTL_DAYS` (``None`` passes).

    The sibling of each writer's ``<= 0`` refusal: it runs before any side effect, so a TTL the CLI
    would not accept cannot reach a row through the library or an import.
    """
    if ttl_days is not None and ttl_days > MAX_TTL_DAYS:
        raise ValueError(
            f"ttl_days={ttl_days} is invalid: it exceeds the maximum of {MAX_TTL_DAYS} days (100 years); "
            "omit the argument or pass None for a permanent entry"
        )


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

    _MAX_TTL_DAYS = MAX_TTL_DAYS

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
