# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Command-tier hook runtime (RDR-215): the ``nx-hook`` entry point and the
shared payload/decision plumbing every hook verb needs.

**This file must stay import-free, and that is the whole point of the
package.** Python runs a package's ``__init__`` before it can reach any
module inside it, so whatever this file imports is paid by every hook
event, on every dispatch, forever. ``nexus.hooks`` is the cautionary
example and the reason this package exists: it imports ``structlog`` and
``nexus.session`` at module scope, so importing ``_io`` while it still lived there -- a
module whose own imports are pure stdlib -- measured 0.06 s against 0.01 s
for a bare ``import nexus`` (nexus-br31l, dev Mac, median of 9). None of
that cost was ``_io``'s; all of it was the parent package's ``__init__``
running first, and no ordering discipline inside ``_io`` or
:mod:`nexus._hook_runtime.entry` could have avoided it.

That mattered because the saving is the whole margin on the cheap hooks.
``phase_review_close_requires_gate`` runs stdlib-only and costs 0.03 s end
to end as bash on this box (the bead records 0.04 s from bead .2's harness;
both are the same order and either makes the point); a port that paid 0.06 s just to reach ``never_fail``
would be a hot-path regression, not the speedup RDR-215 promises. The
expensive hooks are unaffected either way -- ``session-start`` genuinely
needs ``nexus.session`` and pays for it legitimately, against a ~0.8 s CLI
baseline it is already replacing.

What 0.02 s measures is the dispatch FLOOR -- a synthetic stdlib-only
verb through the real entry point. That is the figure the close gate's
COMMON path will pay, the one that runs on every Bash call and exits
early via ``_lib.allow()``. Its narrow phase-review branch additionally
imports ``nexus.session`` and shells out to ``bd show``; that cost is
real, is its own, and stays unmeasured until the port lands.

So: no imports here, not even a convenience re-export, since a re-export
would import the module it re-exports. Import the submodule you want
directly (``from nexus._hook_runtime._io import never_fail``).
``tests/hooks/test_hook_runtime_thin.py`` enforces this mechanically --
both that this file stays import-free and that the modules beside it keep
their module-scope imports inside the standard library -- because the
regression is invisible to every functional test: the wrong import makes
hooks slower, never wrong.
"""
