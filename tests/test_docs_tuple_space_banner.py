# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0miq7.8: the tuple-space docs' banner tracks whether `nx tuple` ships.

``docs/tuple-space.md`` and ``docs/tuple-space-walkthroughs.md`` are written
from the gated RDR-205 design ahead of the code (Phase 0 of the nexus-0miq7
epic). Each opens with a banner stating the routes, tools and verbs it
describes are not yet shipped. That banner is a claim about the CLI, not
decoration, so it has to track the CLI's actual state:

- While ``src/nexus/cli.py`` registers no ``tuple`` command group, both
  pages must still carry the banner's "not yet shipped" phrase -- the
  design-of-record framing is honest only as long as the gap it names is
  real.
- Once ``tuple`` is registered (RDR-205 Phase 2; nexus-0miq7.2 is the docs
  bead that clears the banner), neither page may still carry that phrase --
  a banner claiming something is unshipped when a reader can run it is a
  claim the reader disproves in one command.

Both directions are asserted unconditionally in
``test_banner_correctness_truth_table`` before the live check ever runs, so
the pin is not vacuous in either direction: it fails today if the banner is
removed early, and it will fail the day ``nx tuple`` ships if nobody comes
back to clear it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
PAGES = [
    REPO_ROOT / "docs" / "tuple-space.md",
    REPO_ROOT / "docs" / "tuple-space-walkthroughs.md",
]
BANNER_PHRASE = "not yet shipped"


def _tuple_verb_registered() -> bool:
    """Whether ``src/nexus/cli.py`` currently registers an ``nx tuple``
    command group, resolved against the live Click command tree -- never a
    hand-maintained guess."""
    from nexus.cli import main  # noqa: PLC0415 -- import at call time, not collection time

    return "tuple" in dict(main.commands)


def _banner_is_correct(*, tuple_registered: bool, page_has_banner: bool) -> bool:
    """The banner is correct exactly when its claim ("not yet shipped")
    disagrees with whether ``nx tuple`` is actually registered."""
    return page_has_banner != tuple_registered


def test_banner_correctness_truth_table() -> None:
    """Both directions of the rule, asserted directly and unconditionally,
    so the pin is never vacuous regardless of which state the live CLI is
    in when this runs."""
    assert _banner_is_correct(tuple_registered=False, page_has_banner=True)
    assert not _banner_is_correct(tuple_registered=False, page_has_banner=False)
    assert _banner_is_correct(tuple_registered=True, page_has_banner=False)
    assert not _banner_is_correct(tuple_registered=True, page_has_banner=True)


def test_pages_exist() -> None:
    for page in PAGES:
        assert page.is_file(), f"{page} is missing"


def test_live_banner_tracks_tuple_verb_registration() -> None:
    """The real pin: today's pages against today's CLI. While ``nx tuple``
    does not exist, both pages must carry the banner; once it exists,
    neither may."""
    registered = _tuple_verb_registered()
    for page in PAGES:
        has_banner = BANNER_PHRASE in page.read_text()
        assert _banner_is_correct(tuple_registered=registered, page_has_banner=has_banner), (
            f"{page}: banner present={has_banner}, `nx tuple` registered={registered} -- "
            "the RDR-205 not-yet-shipped banner is now wrong; add it back if `nx tuple` is "
            "still unshipped, or clear it (nexus-0miq7.2) now that it has shipped"
        )
