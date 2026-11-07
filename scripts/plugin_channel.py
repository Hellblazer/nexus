# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The plugin channel allowlist and its helpers (RDR-197, nexus-a2wmi.1).

Under the plugin-only fast lane (the ``release`` skill's "Plugin-only fast
lane" section) this module is the one definition of what a plugin-only release
may ship: :data:`ALLOWED_PREFIXES`, :data:`ALLOWED_EXACT`,
:data:`DENIED_PREFIXES` and :func:`is_channel_path`, which
``scripts/check_plugin_only_eligibility.py`` and ``scripts/plugin_ledger.py``
import rather than copy. The anchored ``plugin-vX.Y.Z-n`` tag channel that
once surrounded it is retired (nexus-rbmja.8) and its helpers are pruned
(nexus-rbmja.9); ``tests/test_plugin_channel_retired.py`` pins their absence.

This module lives under ``scripts/`` deliberately: pyproject's pytest
``pythonpath = ["scripts"]`` makes it importable from tests with no
packaging, and ``scripts/`` is in neither the wheel force-include nor
the sdist include list, so this machinery is off the wheel surface by
construction (pinned by tests/test_plugin_channel.py).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

__all__ = [
    "ALLOWED_EXACT",
    "ALLOWED_PREFIXES",
    "DENIED_PREFIXES",
    "PLUGIN_BY_ALLOWLIST_PREFIX",
    "GitCommandError",
    "TagVisibilityError",
    "assert_tag_visibility",
    "client_version_of",
    "is_channel_path",
    "path_has_prefix",
]


class TagVisibilityError(RuntimeError):
    """The base client tag does not resolve: the checkout is blind to tags."""


class GitCommandError(RuntimeError):
    """A git invocation failed; the answer is unknown, not empty."""


#: The channel allowlist — a strict SUPERSET of the loader-visible
#: SURFACE_BY_PLUGIN in tests/test_plugin_release_drift_ledger.py: it
#: also admits conexus/CHANGELOG.md, conexus/registry.yaml,
#: conexus/evals/, conexus/PENDING_RELEASE.md and every path under sn/,
#: tool-spill/ (the opt-in conexus-tool-spill mod, its README and tests
#: included) and prose-edit/ (the opt-in conexus-prose-edit skill).
#: Do not narrow this to SURFACE_BY_PLUGIN and do not widen
#: SURFACE_BY_PLUGIN to this.
ALLOWED_PREFIXES: tuple[str, ...] = ("conexus/", "sn/", "tool-spill/", "prose-edit/")

ALLOWED_EXACT: tuple[str, ...] = (".claude-plugin/marketplace.json",)

#: Wheel content living inside the plugin trees (hatch force-include
#: ships them as package data), carved OUT of the channel: a plugin cut
#: may never alter wheel behaviour.
DENIED_PREFIXES: tuple[str, ...] = ("conexus/plans/", "conexus/daemon/")

#: Ref movement keys on THIS mapping, not on SURFACE_BY_PLUGIN: a cut
#: carrying only conexus/evals/ or conexus/registry.yaml ships real
#: content the loader-visible surface does not cover, and keying on
#: SURFACE_BY_PLUGIN would move no ref and ship nothing.
#: .claude-plugin/marketplace.json belongs to no plugin and moves no ref.
PLUGIN_BY_ALLOWLIST_PREFIX: dict[str, str] = {
    "conexus/": "conexus",
    "sn/": "sn",
    # The directory is not the plugin name: marketplace.json lists the
    # opt-in mod as conexus-tool-spill, installed from path tool-spill.
    "tool-spill/": "conexus-tool-spill",
    "prose-edit/": "conexus-prose-edit",
}

_CLIENT_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)\Z")


def client_version_of(ref: str) -> str | None:
    """The client version a pinned ref names: ``v{X.Y.Z}`` -> ``X.Y.Z``;
    anything else (the retired anchored ``plugin-v{X.Y.Z}-{n}`` form
    included) -> ``None``.

    The one reader every pin consumer outside the channel should use, so
    none parses the ref shape itself (the a2wmi.12 spike's third refusal
    was a hooks test doing ``int(ref.lstrip("v").split(".")[0])``).
    """
    match = _CLIENT_TAG_RE.match(ref)
    return match.group(1) if match is not None else None


def path_has_prefix(path: str, prefix: str) -> bool:
    """Whether *path* equals *prefix* or sits under it, component-wise.

    The hatch force-include keys are written WITHOUT a trailing slash
    ("conexus/plans") while :data:`DENIED_PREFIXES` carries one, so a
    bare ``startswith`` would report a carved-out path as uncovered —
    and would match "conexus/plansible" against "conexus/plans". Both
    sides are normalised and the match is on a path-component boundary.
    """
    normalised_path = path.rstrip("/")
    normalised_prefix = prefix.rstrip("/")
    return normalised_path == normalised_prefix or normalised_path.startswith(
        normalised_prefix + "/"
    )


def _run_git(
    args: list[str], cwd: str | Path | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )


def assert_tag_visibility(version: str, *, cwd: str | Path | None = None) -> None:
    """The BLIND-CHECKOUT SENTINEL: raise unless ``v{version}`` resolves.

    A shallow checkout that fetched only some refs cannot tell a base client
    tag that is absent from one that was never fetched. The eligibility gate
    judges a diff from that tag, so an unresolvable base proves the checkout
    is blind rather than the diff empty: unreadable must never read as clean.
    """
    probe = _run_git(
        ["rev-parse", "--verify", "--quiet", f"v{version}^{{commit}}"], cwd
    )
    if probe.returncode != 0:
        raise TagVisibilityError(
            f"base client tag v{version} does not resolve: this checkout is "
            f"blind to tags, so a diff from it cannot be trusted. Fetch "
            f"tags before judging it."
        )


def is_channel_path(path: str) -> bool:
    """Whether *path* is content a plugin cut may ship: inside the
    allowlist and not carved out by :data:`DENIED_PREFIXES`. The single
    predicate behind the eligibility gate and the ledger parser (two
    verbatim copies drift)."""
    if any(path_has_prefix(path, denied) for denied in DENIED_PREFIXES):
        return False
    if path in ALLOWED_EXACT:
        return True
    return any(path_has_prefix(path, allowed) for allowed in ALLOWED_PREFIXES)
