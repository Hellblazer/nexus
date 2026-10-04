#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Blocking release-gate: is the engine this release pins current, both ways? (nexus-i5c2u)

Root cause this closes: the release checklist's "Engine-freshness gate" was
prose -- a human had to run ``git log <pinned-engine-tag>..HEAD -- service/``
and judge whether the drift was "non-trivial AND cloud-relevant". That was
skipped in practice: the cloud engine sat at ``engine-service-v0.1.17`` for 9+
days while :data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` moved to
``(0, 1, 34)``. This script is the mechanical check. It fails in BOTH
directions:

* the LIVE cloud engine is behind the pinned floor (probe of the managed
  service's ``/version``, via :func:`nexus.db.managed_endpoint.probe_managed_service`;
  the explicit comparison here is a second, independently testable layer so
  the gate does not silently pass if the probe's own fail-closed behaviour
  ever changes);
* a gated engine tag exists that this release never pinned (local-mode
  installs get ONLY the pinned identity, so an unpinned tag reaches nobody).

After the floor passes, :func:`check_source_ancestry` (nexus-hs4xl) diffs
``service/src/main`` between the pinned tag and HEAD: version numbers can
agree while source disagrees. Exit codes: ``0`` current, ``1`` stale /
incompatible / drift, ``2`` unverifiable (network, git or gh could not
answer -- "could not verify" is never treated as "must be fine").

**Paired-release mode** (``--paired-deploy engine-service-vX.Y.Z``,
nexus-k1c08): under the paired-release choreography (AGENTS.md § Engine-service
release) a client release bumps ``REQUIRED_ENGINE_VERSION`` to an engine tag
whose deploy fires AT client-tag push, so PRE-tag a cloud behind the floor is
the EXPECTED state. The flag is never a default: it names the tag, and
accepts a below-floor cloud only when ALL of these verify independently:
(a) the tag exists in git and is a PUBLISHED GitHub release (non-draft, with
the ``nexus-service-linux-amd64`` asset -- both release matrices run
``fail-fast: false``, so a non-draft release can carry zero native binaries);
(b) ``REQUIRED_ENGINE_VERSION`` equals the tag exactly; (c) it is the newest
published engine tag; (d) its commit was authored within a freshness window
(default 72h, ``--paired-tag-max-age-hours``), so a stable pairing cannot be
reused on a later release. Before those, the wire-contract ledger
(:func:`check_client_lag_ledger`) and the DATA EFFECT relay
(:func:`check_data_effect_relay`) must pass. Any miss keeps the gate red with
a named reason. An unreachable cloud is still exit 2, paired or not, and
only a genuine, parseable below-floor reading counts as "deploy pending"
(:func:`_classify_probe_failure`).

**Auto-paired mode** (``--paired-deploy-auto``, nexus-gc9ir): the unattended
counterpart for ``release.yml``. It derives the tag from
``REQUIRED_ENGINE_VERSION`` and, ONLY when the cloud actually reports below
that floor, runs the identical battery. When the cloud already meets the floor
it is a bare-invocation pass; the paired machinery never runs.

**The core tradeoff:** both paired modes accept on TAG legitimacy, never on
proof the deploy landed. Nothing automated backstops a deploy that never
fired; the post-tag bare re-run of this script is the human VERIFY, and it is
required.

**Ledger-only mode** (``--ledger-only``, nexus-55r6o): just the tree-static
ledger read, for release-branch PR CI.

**Client-precondition mode** (``--client-precondition [TAG]``, nexus-9ssih):
the mirror image, gating an engine DEPLOY rather than a PyPI release -- are
the client commits that ``TAG`` REQUIRES already in a released conexus
version? Checks :data:`ENGINE_CLIENT_PRECONDITIONS` (a hand-filled table,
currently empty), the DATA EFFECT relay, and the same wire-contract ledger.
Exit ``1`` when a required client commit is missing from the latest ``v*``
tag or the ledger has a blocking entry; ``2`` when git cannot be interrogated.
It gates the DEPLOY, never the tag cut.

Usage::

    uv run python scripts/check_engine_release_floor.py
    uv run python scripts/check_engine_release_floor.py --url https://staging.example.com
    uv run python scripts/check_engine_release_floor.py --paired-deploy engine-service-v0.1.63
    uv run python scripts/check_engine_release_floor.py --paired-deploy-auto
    uv run python scripts/check_engine_release_floor.py --ledger-only
    uv run python scripts/check_engine_release_floor.py --client-precondition engine-service-v0.1.61
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import check_wire_contract_pairing as _wire_ledger
import list_data_effects as _data_effects
from nexus.gate_advisory import passed_by_default
from nexus.db.managed_endpoint import (
    ManagedServiceError,
    ManagedServiceUnreachable,
    probe_managed_service,
    resolve_managed_endpoint,
)
from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

_REMEDY = (
    "Remedy: cut + deploy + cloud-gate a fresh engine-service via the "
    "`engine-release` skill (AGENTS.md § Engine-service release), then "
    "re-run this check before cutting the PyPI release."
)


_UNPINNED_REMEDY = (
    "Remedy: bump REQUIRED_ENGINE_VERSION (src/nexus/engine_version.py) to that "
    "tag. That single edit also moves PINNED_SERVICE_TAG, which is DERIVED from "
    "it. If the tag is not deployed to the managed service yet, get conexus to "
    "deploy it FIRST -- bumping ahead of the deploy makes cloud clients refuse "
    "the managed service as below-identity (GH #1402 inverted)."
)

#: Sentinel for "the tag list could not be read". Distinct from "no tags", which
#: is itself a failure -- a repo with zero engine tags cannot be release-gated.
_TAGS_UNAVAILABLE = object()


def newest_published_engine(repo_root: pathlib.Path | None = None) -> object:
    """Highest published ``engine-service-v*`` tag, as a version tuple.

    Returns :data:`_TAGS_UNAVAILABLE` when git cannot be consulted at all. An
    EMPTY tag list is returned as ``None`` and treated as a gate FAILURE by the
    caller, not a pass: in CI ``actions/checkout`` fetches no tags by default,
    and a check that silently passes because it saw nothing is the exact
    vacuous-green failure mode this gate exists to prevent.
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "tag", "-l", "engine-service-v*"],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _TAGS_UNAVAILABLE
    if out.returncode != 0:
        return _TAGS_UNAVAILABLE
    # parse_engine_version takes a VERSION string ("v0.1.56" / "0.1.56"), not the
    # tag form -- strip the namespace prefix first. Getting this wrong makes every
    # tag unparseable, which the empty-list branch below catches as a FAILURE
    # rather than a vacuous pass (it did, on the first run of this code).
    prefix = "engine-service-"
    versions = [
        v for v in (
            parse_engine_version(line.strip()[len(prefix):])
            for line in out.stdout.splitlines()
            if line.strip().startswith(prefix)
        )
        if v is not None
    ]
    return max(versions) if versions else None


def check_pin_currency(newest: object) -> int:
    """Fail when a gated engine tag exists that this release does not pin.

    The OTHER direction of the freshness gate, and the one that had no check at
    all until 2026-07-25. Cloud users get whatever conexus deployed regardless
    of this constant; LOCAL-mode installs get ONLY what REQUIRED_ENGINE_VERSION
    names. So an engine tag that is cut, validated, published -- and never
    pinned -- reaches nobody, while the pre-existing cloud-vs-pin check reports
    "current" and exits 0. That is precisely how the pin sat at v0.1.52 through
    engine tags .53 .54 .55 .56 (found 2026-07-25).

    Hal directive 2026-07-15: ONE engine identity per release, on EVERY install
    path. Not a compatibility minimum, no "only if the release needs the
    features" carve-out -- that carve-out IS the 2026-07-14 v0.1.42 incident.
    """
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    if newest is _TAGS_UNAVAILABLE:
        print(
            "ENGINE PIN CHECK FAILED: could not read engine-service tags from git. "
            "Cannot verify that every gated engine tag is pinned -- treat as a failed "
            "gate, not a pass. In CI, actions/checkout needs `fetch-tags: true`.",
            file=sys.stderr,
        )
        return 2
    if newest is None:
        print(
            "ENGINE PIN CHECK FAILED: zero engine-service-v* tags visible. Either the "
            "checkout has no tags (CI: set `fetch-tags: true`) or the tag namespace "
            "changed. A gate that sees nothing must not report success.",
            file=sys.stderr,
        )
        return 2
    if newest > REQUIRED_ENGINE_VERSION:
        newest_s = ".".join(str(p) for p in newest)
        print(
            f"ENGINE PIN CHECK FAILED: engine-service-v{newest_s} is published but "
            f"this release pins v{floor}. Local-mode installs receive ONLY the pinned "
            f"identity, so every engine fix between v{floor} and v{newest_s} reaches "
            "nobody.\n"
            f"{_UNPINNED_REMEDY}",
            file=sys.stderr,
        )
        return 1
    if newest == REQUIRED_ENGINE_VERSION:
        print(
            f"engine pin is current: REQUIRED_ENGINE_VERSION v{floor} == newest "
            "published tag",
        )
        return 0
    newest_s = ".".join(str(p) for p in newest)
    print(
        f"engine pin is ahead of publication: REQUIRED_ENGINE_VERSION v{floor} "
        "names no published engine-service tag -- the newest published is v"
        f"{newest_s}. Not a failure (the paired-release choreography cuts the "
        "engine tag before bumping this pin), but the pin is NOT \"current\" "
        "against any published tag yet.",
    )
    return 0


def _tag_exists_in_git(tag: str, repo_root: pathlib.Path | None = None) -> object:
    """``True``/``False``, or :data:`_TAGS_UNAVAILABLE` if git could not answer.

    Same exact-match idiom as :func:`newest_published_engine` (``git tag -l``),
    but for a single named tag rather than the whole ``engine-service-v*``
    namespace -- paired mode needs to know THIS tag exists, not just the
    newest one.
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "tag", "-l", tag],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _TAGS_UNAVAILABLE
    if out.returncode != 0:
        return _TAGS_UNAVAILABLE
    return tag in out.stdout.split()


#: The specific artifact conexus deploy consumes (workflow release-notes
#: comment, engine-service-release.yml ~line 125): "Consumed by conexus
#: deploy/engine (build the runtime image FROM the linux-amd64 binary)".
#: Both the native-binary and PG-bundle matrices run ``fail-fast: false``, so
#: a release can be non-draft with real assets attached while carrying ZERO
#: native binaries -- "assets non-empty" does not prove the artifact the
#: pairing is meant to certify has actually landed; this specific name does.
_REQUIRED_ASSET_NAME = "nexus-service-linux-amd64"


def _paired_tag_published(tag: str, repo_root: pathlib.Path | None = None) -> tuple[object, str]:
    """Verify ``tag`` has a PUBLISHED GitHub release carrying the deploy asset.

    "Published" here means: non-draft, AND an asset named
    :data:`_REQUIRED_ASSET_NAME` is present (not merely "some asset exists" --
    see that constant's docstring for why a bare non-empty check is too weak).

    ``repo_root`` anchors the ``gh`` call the same way :func:`_tag_exists_in_git`
    and :func:`newest_published_engine` anchor their ``git`` calls -- without
    it, ``gh`` silently resolves whatever repository it auto-detects from the
    process's actual cwd rather than failing closed the way the git helpers
    do, which is inconsistent with this module's own doctrine.

    Returns ``(True, "")`` on success, ``(False, reason)`` on a verified
    mismatch (draft / missing asset), or ``(_TAGS_UNAVAILABLE, reason)`` when
    ``gh`` could not be consulted at all (missing binary, auth failure,
    non-JSON output, or a response missing the fields needed to judge it). The
    last case is fail-closed by construction -- same doctrine as
    :data:`_TAGS_UNAVAILABLE` elsewhere in this module: an unverifiable
    publication state is a failed gate, not a pass.
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["gh", "release", "view", tag, "--json", "isDraft,assets"],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            _TAGS_UNAVAILABLE,
            f"could not invoke `gh` to verify the release ({exc}) -- install "
            "the GitHub CLI and run `gh auth login`, or verify GH_TOKEN is set",
        )
    if out.returncode != 0:
        detail = out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"
        return _TAGS_UNAVAILABLE, f"`gh release view {tag}` failed: {detail}"
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        return _TAGS_UNAVAILABLE, f"`gh release view {tag}` returned unparseable JSON ({exc})"

    # A missing `isDraft` key is UNVERIFIABLE, not "must be non-draft" --
    # `payload.get("isDraft")` defaulting falsy on absence would silently
    # treat "gh's response shape changed" as a pass, the exact vacuous-green
    # failure mode this module exists to avoid.
    if "isDraft" not in payload:
        return (
            _TAGS_UNAVAILABLE,
            f"`gh release view {tag}` response has no isDraft field -- cannot "
            "verify publication state",
        )
    if payload["isDraft"]:
        return False, f"release {tag} is still a DRAFT -- not published"

    asset_names = {
        a.get("name") for a in (payload.get("assets") or []) if isinstance(a, dict)
    }
    if _REQUIRED_ASSET_NAME not in asset_names:
        present = ", ".join(sorted(n for n in asset_names if n)) or "none"
        return (
            False,
            f"release {tag} has no `{_REQUIRED_ASSET_NAME}` asset -- the "
            f"binary conexus deploy actually consumes has not landed (assets "
            f"present: {present})",
        )
    return True, ""


#: Deploy fires AT client-tag push (paired-release choreography, Hal
#: directive 2026-08-02) -- a pairing older than this is not THIS release's
#: partner. Overridable ONLY via the explicit ``--paired-tag-max-age-hours``
#: flag (nexus-k1c08 fix round, critique CRITICAL 1): without a bound, (a)-
#: (c) are stable facts that stay true indefinitely once armed, so a reused
#: ``--paired-deploy`` on a LATER release would get the identical acceptance
#: forever -- reopening the i5c2u multi-release drift class this gate exists
#: to close.
_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS = 72.0

#: How far into the future the paired tag's commit date may sit before it is
#: refused. A freshness bound refuses what is too OLD and says nothing about
#: what is too NEW, so a date ahead of now would satisfy it forever; a commit
#: author date is whatever machine authored it. Fifteen minutes is generous
#: for NTP-synced hosts and too short to buy a meaningful window. This is NOT
#: a second freshness constant -- the window itself stays
#: :data:`_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS`.
_FUTURE_CLOCK_TOLERANCE_HOURS = 0.25


def _tag_age_hours(tag: str, repo_root: pathlib.Path | None = None) -> object:
    """Hours since ``tag``'s target commit was authored, or :data:`_TAGS_UNAVAILABLE`.

    Reads the commit's AUTHOR date (``git log -1 --format=%aI <tag>``, cwd-
    anchored like this module's other git calls) rather than the tag ref's
    own creation timestamp: annotated vs lightweight tags complicate
    ``git for-each-ref``'s ``creatordate``, and "how long ago was the engine
    work this tag represents cut" -- which the author date answers directly --
    is what the paired-release choreography's "deploy fires AT tag push" is
    actually bounding.
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%aI", tag],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _TAGS_UNAVAILABLE
    if out.returncode != 0:
        return _TAGS_UNAVAILABLE
    raw = out.stdout.strip()
    if not raw:
        return _TAGS_UNAVAILABLE
    try:
        tagged_at = datetime.fromisoformat(raw)
    except ValueError:
        return _TAGS_UNAVAILABLE
    if tagged_at.tzinfo is None:
        tagged_at = tagged_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - tagged_at
    return age.total_seconds() / 3600.0


#: Scope for the source-ancestry arm (nexus-hs4xl): PRODUCTION source only.
#: Test-only and pom-only churn between an engine tag and HEAD is routine and
#: does NOT mean the pin is stale -- scoping wider would make this arm cry
#: wolf on every dependency bump or test refactor (bead nexus-hs4xl design
#: question 1). ``service/src/main`` covers both Java sources AND
#: non-code artifacts that are equally load-bearing for the deployed
#: engine's behavior: Liquibase changelogs live under
#: ``src/main/resources``, and a schema change ships exactly as much
#: undeployed behavior as a Java diff does.
_ANCESTRY_SCOPE = "service/src/main"


def _pinned_engine_tag() -> str:
    """The floor's tag string -- derived, so it cannot drift from the constant."""
    return "engine-service-v" + ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)


def check_source_ancestry(pinned_tag: str, repo_root: pathlib.Path | None = None) -> int:
    """The nexus-hs4xl arm: version NUMBERS can agree while SOURCE disagrees.

    v7.6.1 pinned ``engine-service-v0.1.71`` -- current at the time, by
    version number -- while ALSO carrying 156 insertions of
    ``service/src/main`` Java that v0.1.71's tag does not contain
    (``CatalogRepository.java``, ``StagingPromoteOps.java``,
    ``VectorHandler.java``, ``PgVectorRepository.java`` -- the RDR-191
    F10c producer fixes). :func:`check_pin_currency` and the cloud probe in
    :func:`check_floor` both passed: three-way version agreement (floor,
    newest published tag, deployed cloud), zero source-tree comparison. This
    closes that gap: for the tag actually being pinned -- or the
    ``--paired-deploy`` tag when armed, which LEGITIMATELY carries service
    source destined for the tag being cut in parallel, see the module
    docstring's paired-mode section -- diff the ACTUAL source tree between
    the pinned tag and HEAD within :data:`_ANCESTRY_SCOPE`. Non-empty means
    the release ships engine behavior its own pinned tag does not contain.

    Reuses :func:`_tag_exists_in_git` for the same fail-closed existence
    check the paired-mode preconditions already rely on -- a checkout
    missing the tag (shallow clone without ``fetch-depth: 0``, or the tag
    genuinely does not exist) is UNVERIFIABLE, never a silent pass.

    Returns ``0`` clean, ``1`` drift detected (named files in the message),
    ``2`` unverifiable (tag missing / git failure -- "could not verify" is
    never "must be fine", same doctrine as the rest of this module).
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    exists = _tag_exists_in_git(pinned_tag, repo_root=root)
    if exists is _TAGS_UNAVAILABLE:
        print(
            "ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: could not confirm "
            f"{pinned_tag} exists in git. Cannot compare source trees -- treat as a "
            "failed gate, not a pass. In CI, actions/checkout needs `fetch-depth: 0` "
            "(release.yml already sets this).",
            file=sys.stderr,
        )
        return 2
    if not exists:
        print(
            f"ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: {pinned_tag} does not exist "
            "in this checkout's git history. Cannot compare source trees -- treat as "
            "a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    try:
        out = subprocess.run(
            ["git", "diff", "--stat", pinned_tag, "HEAD", "--", _ANCESTRY_SCOPE],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: git diff failed ({exc}). "
            "Cannot compare source trees -- treat as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    if out.returncode != 0:
        print(
            f"ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: `git diff {pinned_tag} HEAD "
            f"-- {_ANCESTRY_SCOPE}` exited {out.returncode}: {out.stderr.strip()}",
            file=sys.stderr,
        )
        return 2
    diff = out.stdout.strip()
    if diff:
        print(
            "ENGINE SOURCE-ANCESTRY CHECK FAILED: this release ships "
            f"{_ANCESTRY_SCOPE} source that its pinned engine tag ({pinned_tag}) does "
            "not contain:\n"
            f"{diff}\n"
            "The floor is version-CURRENT but SOURCE-STALE: a pin equal to the newest "
            "published tag can still predate shipped engine source (nexus-ajlz5). Cut "
            "a fresh engine tag carrying this source (or re-pin to a tag that already "
            "does) before releasing -- see AGENTS.md § Engine-service release, "
            "paired-release choreography.",
            file=sys.stderr,
        )
        return 1
    print(
        f"engine source is current: no {_ANCESTRY_SCOPE} drift between "
        f"{pinned_tag} and HEAD",
    )
    return 0


def check_client_lag_ledger() -> int:
    """The both-halves wire-contract ledger gate (nexus-1vogq). The engine-side
    complement to ``scripts/check_wire_contract_pairing.py``'s static tripwire:
    this is where the DEPLOY relay itself surfaces an unshipped client half BY
    NAME, rather than relying on someone having read the ledger prose.

    A non-empty ``## Unshipped`` section blocks unless every entry carries the
    leading ``[additive]`` direction-safety token (nexus-1emxn choreography
    (a): old client + new engine is safe, so the engine may deploy ahead of the
    client tag). The token is interpreted in ONE place,
    ``check_wire_contract_pairing.classify_unshipped`` (nexus-hcdk3), shared
    by every mode of this script.

    Returns ``0`` (ledger empty, or every entry additive) or ``1`` (blocking
    entries present -- named in the message).
    """
    ledger = _wire_ledger.parse_ledger(_wire_ledger.DEFAULT_LEDGER_PATH)
    if not ledger.unshipped:
        print(
            "client-lag ledger clean: 0 unshipped both-halves commits in "
            f"{_wire_ledger.DEFAULT_LEDGER_PATH}",
        )
        return 0

    verdict = _wire_ledger.classify_unshipped(ledger)
    if verdict.blocking:
        entries = "\n".join(
            f"  {e.sha}  bead {e.bead}  engine tag {e.engine_tag}  ({e.note})"
            for e in verdict.blocking
        )
        print(
            f"PAIRED DEPLOY BLOCKED: {len(verdict.blocking)} both-halves commit(s) in "
            f"{_wire_ledger.DEFAULT_LEDGER_PATH} have an unshipped client half and no "
            "[additive] direction-safety token:\n"
            f"{entries}\n"
            "\n"
            "This engine tag cannot deploy ahead of the client release carrying "
            "the listed commit(s) (nexus-1vogq). Pair this deploy with that "
            "client release.",
            file=sys.stderr,
        )
        return 1

    beads = ", ".join(sorted(e.bead for e in verdict.additive))
    print(
        f"client-lag ledger: {len(verdict.additive)} unshipped both-halves "
        "commit(s), all marked [additive] (old client + new engine safe) -- "
        "deploy authorized ahead of the client tag (nexus-1emxn choreography "
        f"(a)); pairing completes when the client release carrying {beads} "
        "bumps the floor.",
    )
    print(
        passed_by_default(
            "check_client_lag_ledger",
            "every unshipped both-halves commit carries the [additive] token; the "
            "deploy is authorized on that token alone, with no paired client tag "
            "verified",
        ),
    )
    return 0


def check_paired_preconditions(
    tag: str,
    newest: object,
    max_age_hours: float = _DEFAULT_PAIRED_TAG_MAX_AGE_HOURS,
) -> int:
    """Verify ``--paired-deploy TAG`` is a legitimately armed pairing (nexus-k1c08).

    ALL of the following must hold or the gate stays red with a named reason:

    (a) ``tag`` exists in git AND is a PUBLISHED (non-draft, with the
        :data:`_REQUIRED_ASSET_NAME` asset) GH release -- the
        ``engine-service-release`` workflow's signed-binary output.
    (b) ``REQUIRED_ENGINE_VERSION`` equals the tag's parsed version EXACTLY.
    (c) ``tag`` is the newest published ``engine-service-v*`` tag -- a newer
        tag than the pairing means unaccounted engine work; keep the
        pin-currency red rather than silently accept it.
    (d) ``tag``'s commit was authored within ``max_age_hours`` of now (default
        :data:`_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS`) -- (a)-(c) are otherwise
        STABLE facts that never expire on their own, so without this bound a
        reused ``--paired-deploy`` on a later release would pass forever; see
        :data:`_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS`'s docstring.

    Returns ``0`` when armed, ``2`` when unverifiable (git/gh unavailable --
    same fail-closed doctrine as the rest of this module: "could not check"
    is never treated as "must be fine"), ``1`` when verifiably mismatched
    (draft / missing asset / wrong tag shape / wrong version / stale pairing
    / too old).
    """
    prefix = "engine-service-"
    if not tag.startswith(prefix):
        print(
            f"PAIRED MODE REJECTED: --paired-deploy {tag!r} is not an "
            "engine-service-v* tag.",
            file=sys.stderr,
        )
        return 1
    parsed_tag = parse_engine_version(tag[len(prefix):])
    if parsed_tag is None:
        print(
            f"PAIRED MODE REJECTED: --paired-deploy {tag!r} does not parse as a "
            "version.",
            file=sys.stderr,
        )
        return 1

    exists = _tag_exists_in_git(tag)
    if exists is _TAGS_UNAVAILABLE:
        print(
            f"PAIRED MODE UNVERIFIABLE: could not read git tags to confirm {tag} "
            "exists. Cannot verify the pairing -- treat as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    if not exists:
        print(
            f"PAIRED MODE REJECTED: {tag} does not exist in git. --paired-deploy must "
            "name a tag that has actually been pushed.",
            file=sys.stderr,
        )
        return 1

    published, reason = _paired_tag_published(tag)
    if published is _TAGS_UNAVAILABLE:
        print(
            f"PAIRED MODE UNVERIFIABLE: {reason}. Cannot verify publication -- treat "
            "as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    if not published:
        print(f"PAIRED MODE REJECTED: {reason}.", file=sys.stderr)
        return 1

    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    tag_s = ".".join(str(p) for p in parsed_tag)
    if parsed_tag != REQUIRED_ENGINE_VERSION:
        print(
            f"PAIRED MODE REJECTED: --paired-deploy names v{tag_s} but "
            f"REQUIRED_ENGINE_VERSION is v{floor} -- wrong pairing. The flag must name "
            "the exact tag this release pairs with.",
            file=sys.stderr,
        )
        return 1

    if newest is _TAGS_UNAVAILABLE:
        print(
            "PAIRED MODE UNVERIFIABLE: could not read engine-service tags from git to "
            "confirm no newer tag exists.",
            file=sys.stderr,
        )
        return 2
    if newest is None or newest != parsed_tag:
        newest_s = ".".join(str(p) for p in newest) if newest is not None else "none"
        print(
            f"PAIRED MODE REJECTED: newest published engine tag is v{newest_s}, not v"
            f"{tag_s} -- a newer engine tag exists than the one this release pairs "
            "with; unaccounted engine work. Keep the pin-currency red until it is "
            "pinned or explained.",
            file=sys.stderr,
        )
        return 1

    age_hours = _tag_age_hours(tag)
    if age_hours is _TAGS_UNAVAILABLE:
        print(
            f"PAIRED MODE UNVERIFIABLE: could not determine {tag}'s commit age from "
            "git. Cannot verify the pairing is fresh -- treat as a failed gate, not a "
            "pass.",
            file=sys.stderr,
        )
        return 2
    if age_hours < -_FUTURE_CLOCK_TOLERANCE_HOURS:
        # The freshness bound is one-sided, and a commit author date is
        # settable to anything, so a future-dated tag would satisfy (d) forever.
        print(
            f"PAIRED MODE REJECTED: {tag}'s commit date is {-age_hours:.1f}h in the "
            f"FUTURE, past the {_FUTURE_CLOCK_TOLERANCE_HOURS:.2f}h skew tolerance. A "
            "commit author date is settable to anything, and the freshness window "
            "only refuses what is too OLD -- a future-dated tag would satisfy it "
            "forever.",
            file=sys.stderr,
        )
        return 1
    if age_hours > max_age_hours:
        print(
            f"PAIRED MODE REJECTED: {tag} is {age_hours:.1f}h old, past the "
            f"{max_age_hours:.1f}h paired-tag freshness window. Deploy fires AT "
            "client-tag push -- a pairing this old is not THIS release's partner, and "
            "accepting it reopens the multi-release i5c2u drift class this gate "
            "exists to close. If this release genuinely lagged its engine tag, "
            "override explicitly with --paired-tag-max-age-hours.",
            file=sys.stderr,
        )
        return 1

    print(
        f"paired mode ARMED: {tag} verified published, pinned to "
        "REQUIRED_ENGINE_VERSION, newest published engine tag, and "
        f"{age_hours:.1f}h old (within the {max_age_hours:.1f}h window).",
    )
    return 0


def _classify_probe_failure(exc: ManagedServiceError) -> tuple[bool, str]:
    """Distinguish a GENUINE below-floor cloud report from any other probe
    failure inside a caught :class:`ManagedServiceError` (nexus-gc9ir review
    round, SIGNIFICANT finding 3).

    :func:`~nexus.db.managed_endpoint.probe_managed_service` raises
    ``ManagedServiceIncompatible`` for FIVE distinct reasons (see its
    docstring): a non-200 status, a non-JSON body, a missing/unparseable
    ``release_version``, AND the one that matters here -- a
    ``release_version`` that parsed fine but sits numerically below
    ``REQUIRED_ENGINE_VERSION``. Only that LAST raise site populates the
    exception's structured ``deployed_version`` field (nexus-b6qlf Fix 2);
    every other raise site leaves it ``None``. Folding all five into
    "accept as paired, deploy is just pending" -- the bug both paired call
    sites carried before this fix -- would let a genuinely broken or
    misconfigured endpoint sail through paired mode (explicit OR auto) as
    if it were merely a pre-deploy cloud, which is exactly the
    "unverifiable is never a pass" doctrine this module exists to enforce.

    Returns ``(True, deployed_version)`` for a genuine below-floor report;
    ``(False, "")`` for anything else. The caller MUST treat the ``False``
    case as UNVERIFIABLE (exit 2), never an acceptance -- paired or not.
    """
    deployed = getattr(exc, "deployed_version", None)
    return (deployed is not None, deployed or "")


def _previous_engine_tag(tag: str, repo_root: pathlib.Path) -> str | None:
    """The published ``engine-service-v*`` tag immediately BEFORE *tag* by
    parsed version, or ``None`` when *tag* is the oldest (or only) one --
    there is nothing to diff a DATA EFFECT range against in that case."""
    prefix = "engine-service-"
    try:
        out = subprocess.run(
            ["git", "tag", "-l", "engine-service-v*"],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    parsed_tag = parse_engine_version(tag[len(prefix):]) if tag.startswith(prefix) else None
    if parsed_tag is None:
        return None
    older = [
        (v, line.strip())
        for line in out.stdout.splitlines()
        if line.strip().startswith(prefix)
        and (v := parse_engine_version(line.strip()[len(prefix):])) is not None
        and v < parsed_tag
    ]
    if not older:
        return None
    return max(older, key=lambda pair: pair[0])[1]


def check_data_effect_relay(tag: str, repo_root: pathlib.Path | None = None) -> int:
    """Refuse the paired battery unless *tag*'s DATA EFFECT relay table was
    recorded (nexus-iu43o) -- the machine-checked half of the engine-
    release skill's handoff step, wired into the actual gate rather than
    left as prose only (which is the exact gap this bead exists to close).

    Delegates entirely to :func:`list_data_effects.verify_relay_attestation`
    for the from_tag..tag range this function derives itself (the previous
    published engine tag) -- REFUSE (1) on a missing or stale attestation,
    PASS (0) when it matches, and PASS (0), NOT-APPLICABLE, both when the
    range has no data-effecting changesets at all AND when *tag* is the
    oldest published engine tag (no range to compute at all).
    """
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    from_tag = _previous_engine_tag(tag, root)
    if from_tag is None:
        print(
            f"NOT-APPLICABLE: {tag} has no earlier published engine-service-v* tag "
            "to diff a DATA EFFECT range against."
        )
        return 0
    return _data_effects.verify_relay_attestation(from_tag, tag, root)


def _run_paired_precondition_battery(
    tag: str,
    newest: object,
    paired_tag_max_age_hours: float,
) -> int:
    """The local, no-network battery an ARMED pairing must clear -- shared by
    BOTH explicit ``--paired-deploy`` and auto-derived ``--paired-deploy-auto``
    (nexus-gc9ir): one function, two call sites, so a change to the battery
    cannot land in only one mode by accident.

    Order: the both-halves wire-contract ledger (nexus-1vogq) FIRST -- local,
    no network -- THEN :func:`check_paired_preconditions` (nexus-k1c08), THEN
    :func:`check_data_effect_relay` (nexus-iu43o).

    Returns 0 when all pass; the first failing check's own named-reason exit
    code otherwise.
    """
    ledger_rc = check_client_lag_ledger()
    if ledger_rc != 0:
        return ledger_rc
    paired_rc = check_paired_preconditions(
        tag, newest, max_age_hours=paired_tag_max_age_hours
    )
    if paired_rc != 0:
        return paired_rc
    return check_data_effect_relay(tag)


def _paired_below_floor_path(
    deployed_version: str,
    newest: object,
    paired_tag_max_age_hours: float,
    *,
    probe: str,
) -> int:
    """Shared tail of auto-paired mode once the cloud is confirmed below floor.

    Runs :func:`_run_paired_precondition_battery` -- the IDENTICAL local
    battery :func:`check_floor` runs for an explicit ``--paired-deploy`` --
    on the tag AUTO-derived from ``REQUIRED_ENGINE_VERSION``. On success,
    emits the auto-paired acknowledgment row (``check_floor_auto_paired::
    auto_*_ack``) and returns 0; any precondition miss returns its own
    named-reason code unchanged.

    ``probe``: whether the caller reduced its probe result to a probe error
    (``"ms_error_below_floor"``) or a successful below-floor read
    (``"success_below_floor"``); this shared tail cannot tell the two apart on
    its own, and the advisory line names which one the pass rests on.
    """
    tag = _pinned_engine_tag()
    paired_rc = _run_paired_precondition_battery(
        tag, newest, paired_tag_max_age_hours
    )
    if paired_rc != 0:
        return paired_rc
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    print(
        f"PAIRED MODE: cloud reports release_version {deployed_version!r}, behind "
        f"floor v{floor}. Expected pre-deploy under the paired-release "
        "choreography -- the deploy fires at client-tag push (AGENTS.md § Cutting "
        "a release, step 0), not before this tag exists. Pairing AUTO-derived "
        "from REQUIRED_ENGINE_VERSION (--paired-deploy-auto, nexus-gc9ir) -- no "
        "explicit --paired-deploy given.\n"
        "POST-TAG VERIFY REQUIRED: re-run this script WITHOUT --paired-deploy "
        "once the deploy lands, to confirm the cloud engine actually converged -- "
        "escalate loudly (never silently re-accept) if it is still behind at that "
        "point.",
    )
    saw = "probe failed" if probe == "ms_error_below_floor" else "answered"
    print(
        passed_by_default(
            "check_floor_auto_paired",
            f"the cloud {saw} below floor and the pass rests on the auto-derived "
            "paired tag, not on a live engine at floor",
        ),
    )
    return 0


def _check_floor_auto_paired(
    url: str | None,
    newest: object,
    paired_tag_max_age_hours: float,
) -> int:
    """``--paired-deploy-auto`` (nexus-gc9ir): probe the cloud FIRST to decide
    which path to take.

    Unlike explicit ``--paired-deploy`` -- which always demands the named
    tag's preconditions hold before ever looking at the cloud, even if the
    cloud already caught up -- auto mode's contract is "must not weaken
    anything": when the cloud already meets the floor this MUST be a byte-
    for-byte bare-invocation pass (pin-currency, then the ordinary "current"
    message), with the paired machinery (ledger, git/gh tag verification)
    never invoked. That decision can only be made after probing, so auto
    mode probes once, up front, and branches on the result -- rather than
    reusing :func:`check_floor`'s normal probe-after-local-checks ordering.

    An unreachable cloud stays exit 2 regardless of mode -- unverifiable is
    never a pass, auto or not.
    """
    base = url or resolve_managed_endpoint(require_token=False)[0]
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)

    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceUnreachable as exc:
        print(
            f"ENGINE FLOOR CHECK FAILED: managed service at {base} is unreachable ("
            f"{exc}). Cannot verify the cloud engine version -- treat this as a failed "
            "gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    except ManagedServiceError as exc:
        # Only a GENUINE, parseable below-floor version reading is "deploy
        # pending" for auto mode's purposes -- an endpoint error or
        # malformed response is UNVERIFIABLE, never folded into paired
        # acceptance (nexus-gc9ir review round, SIGNIFICANT finding 3; see
        # _classify_probe_failure).
        is_below_floor, deployed = _classify_probe_failure(exc)
        if not is_below_floor:
            print(
                f"ENGINE FLOOR CHECK UNVERIFIABLE (required v{floor}): managed service at "
                f"{base} probe failed without a genuine below-floor version reading ({exc}"
                "). Paired mode only ever accepts a GENUINE, parseable below-floor "
                "version report as 'deploy pending' -- an endpoint error or a "
                "malformed/unparseable response is never folded into that acceptance, "
                "paired or not. Treat as a failed gate, not a pass.",
                file=sys.stderr,
            )
            return 2
        return _paired_below_floor_path(
            deployed, newest, paired_tag_max_age_hours, probe="ms_error_below_floor",
        )

    parsed = parse_engine_version(caps.release_version)
    if parsed is not None and parsed >= REQUIRED_ENGINE_VERSION:
        # Cloud already meets the floor: EXACTLY the bare-invocation path.
        pin_rc = check_pin_currency(newest)
        if pin_rc != 0:
            return pin_rc
        print(
            f"cloud engine is current: {caps.base_url} release_version="
            f"{caps.release_version} (floor v{floor})",
        )
        return 0

    if parsed is None:
        # Reachable, but the response carries an unparseable release_version
        # -- never reachable via the REAL probe (which raises before ever
        # returning such a caps; see probe_managed_service's docstring), but
        # for defense in depth this must not silently fold into paired
        # acceptance either -- same "genuine below-floor only" rule as the
        # exception branch above.
        print(
            f"ENGINE FLOOR CHECK UNVERIFIABLE (required v{floor}): managed service at "
            f"{base} reported an unparseable release_version {caps.release_version!r}. "
            "Paired mode only ever accepts a GENUINE, parseable below-floor version "
            "report as 'deploy pending' -- an endpoint error or a "
            "malformed/unparseable response is never folded into that acceptance, "
            "paired or not. Treat as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2

    # Reachable, with a genuine parseable release_version below the floor.
    return _paired_below_floor_path(
        caps.release_version, newest, paired_tag_max_age_hours, probe="success_below_floor",
    )


def check_floor(
    url: str | None = None,
    newest: object | None = None,
    paired_deploy: str | None = None,
    paired_deploy_auto: bool = False,
    paired_tag_max_age_hours: float = _DEFAULT_PAIRED_TAG_MAX_AGE_HOURS,
) -> int:
    """Probe the live managed service and compare against the version floor.

    Returns an exit code (0 = current, non-zero = stale or unverifiable).
    Never raises: every failure mode of the probe (unreachable, incompatible,
    or any other :class:`~nexus.db.managed_endpoint.ManagedServiceError`) is
    caught here and turned into a clear stderr message plus non-zero exit --
    an unrelated network blip must fail the gate loudly, not crash with an
    unhandled traceback and definitely not report success.

    ``paired_deploy`` (nexus-k1c08): when set, replaces the ordinary
    pin-currency check with :func:`check_paired_preconditions`, and a
    below-floor (or unparseable) cloud result is ACCEPTED with an explicit
    acknowledgment instead of failing the gate -- see the module docstring.
    ``None`` (the default) takes the exact pre-k1c08 code path, unchanged.
    ``paired_tag_max_age_hours`` bounds how old the paired tag's commit may
    be (see :data:`_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS`); ignored when
    ``paired_deploy`` is ``None``.

    ``paired_deploy_auto`` (nexus-gc9ir): when ``True`` AND ``paired_deploy``
    is ``None``, delegates entirely to :func:`_check_floor_auto_paired`,
    which derives the candidate tag from ``REQUIRED_ENGINE_VERSION`` and
    probes the cloud FIRST to decide whether the bare or the paired path
    applies -- see the module docstring's auto-paired section. An explicit
    ``paired_deploy`` always takes priority over ``paired_deploy_auto`` when
    both are somehow set (the CLI enforces mutual exclusion; this is the
    library-level tiebreak). ``False`` (the default) leaves every existing
    code path byte-for-byte unchanged.
    """
    resolved_newest = newest_published_engine() if newest is None else newest

    if paired_deploy_auto and paired_deploy is None:
        return _check_floor_auto_paired(
            url=url,
            newest=resolved_newest,
            paired_tag_max_age_hours=paired_tag_max_age_hours,
        )

    if paired_deploy is not None:
        # _run_paired_precondition_battery: ledger (nexus-1vogq) FIRST -- local,
        # no network, same "cheap local check before anything else" ordering as
        # pin-currency below -- THEN check_paired_preconditions (nexus-k1c08).
        # Shared with auto mode's tail (nexus-gc9ir) so the two entry points
        # can never drift on what "armed" means.
        paired_rc = _run_paired_precondition_battery(
            paired_deploy, resolved_newest, paired_tag_max_age_hours
        )
        if paired_rc != 0:
            return paired_rc
    else:
        # Pin-currency FIRST: local, no network, and a failure here is
        # actionable without contacting anything. The cloud probe follows.
        pin_rc = check_pin_currency(resolved_newest)
        if pin_rc != 0:
            return pin_rc

    base = url or resolve_managed_endpoint(require_token=False)[0]
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)

    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceUnreachable as exc:
        # Unreachable stays a hard failure regardless of pairing -- "could not
        # verify" is never treated as "must be fine", paired or not.
        print(
            f"ENGINE FLOOR CHECK FAILED: managed service at {base} is unreachable ("
            f"{exc}). Cannot verify the cloud engine version -- treat this as a failed "
            "gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    except ManagedServiceError as exc:
        # probe_managed_service already fails closed on a below-floor / missing
        # / unparseable release_version -- its message names the deployed
        # version and the floor already, so surface it verbatim plus the
        # remedy pointer. In paired mode a GENUINE below-floor reading is the
        # EXPECTED pre-deploy state, not a defect -- accept it with the
        # explicit acknowledgment. An endpoint error or malformed response is
        # NOT "deploy pending" and must stay UNVERIFIABLE even in paired mode
        # (nexus-gc9ir review round, SIGNIFICANT finding 3 -- this branch
        # used to fold every ManagedServiceError into acceptance; see
        # _classify_probe_failure).
        if paired_deploy is not None:
            # This IS the real production paired-acceptance path (the
            # below-floor probe raises ManagedServiceIncompatible, a
            # ManagedServiceError subclass, here -- not in the dead
            # post-probe comparison below, which only patched tests reach).
            is_below_floor, deployed = _classify_probe_failure(exc)
            if not is_below_floor:
                print(
                    f"ENGINE FLOOR CHECK UNVERIFIABLE (required v{floor}): managed service at "
                    f"{base} probe failed without a genuine below-floor version reading ({exc}"
                    "). Paired mode only ever accepts a GENUINE, parseable below-floor "
                    "version report as 'deploy pending' -- an endpoint error or a "
                    "malformed/unparseable response is never folded into that acceptance, "
                    "paired or not. Treat as a failed gate, not a pass.",
                    file=sys.stderr,
                )
                return 2
            print(
                f"PAIRED MODE: cloud reports release_version {deployed!r}, behind floor v"
                f"{floor}. Expected pre-deploy under the paired-release choreography -- "
                "the deploy fires at client-tag push (AGENTS.md § Cutting a release, step "
                "0), not before this tag exists. Pairing named via --paired-deploy.\n"
                "POST-TAG VERIFY REQUIRED: re-run this script WITHOUT --paired-deploy "
                "once the deploy lands, to confirm the cloud engine actually converged -- "
                "escalate loudly (never silently re-accept) if it is still behind at that "
                "point.",
            )
            print(
                passed_by_default(
                    "check_floor_paired",
                    "the cloud probe failed below floor and the pass rests on the verified "
                    "--paired-deploy tag, not on a live engine at floor",
                ),
            )
            return 0
        print(
            f"ENGINE FLOOR CHECK FAILED (required v{floor}): {exc}\n"
            f"{_REMEDY}",
            file=sys.stderr,
        )
        return 1

    parsed = parse_engine_version(caps.release_version)
    if parsed is None or parsed < REQUIRED_ENGINE_VERSION:
        if paired_deploy is not None:
            if parsed is None:
                # Reachable, but an unparseable release_version -- never
                # reachable via the REAL probe (see probe_managed_service's
                # docstring), but for defense in depth this must not
                # silently fold into paired acceptance either.
                print(
                    f"ENGINE FLOOR CHECK UNVERIFIABLE (required v{floor}): managed service at "
                    f"{base} reported an unparseable release_version {caps.release_version!r}. "
                    "Paired mode only ever accepts a GENUINE, parseable below-floor version "
                    "report as 'deploy pending' -- an endpoint error or a "
                    "malformed/unparseable response is never folded into that acceptance, "
                    "paired or not. Treat as a failed gate, not a pass.",
                    file=sys.stderr,
                )
                return 2
            print(
                f"PAIRED MODE: cloud reports release_version {caps.release_version!r}, "
                f"behind floor v{floor}. Expected pre-deploy under the paired-release "
                "choreography -- the deploy fires at client-tag push (AGENTS.md § Cutting "
                "a release, step 0), not before this tag exists. Pairing named via "
                "--paired-deploy.\n"
                "POST-TAG VERIFY REQUIRED: re-run this script WITHOUT --paired-deploy "
                "once the deploy lands, to confirm the cloud engine actually converged -- "
                "escalate loudly (never silently re-accept) if it is still behind at that "
                "point.",
            )
            print(
                passed_by_default(
                    "check_floor_paired",
                    "the cloud answered below floor and the pass rests on the verified "
                    "--paired-deploy tag, not on a live engine at floor",
                ),
            )
            return 0
        print(
            f"ENGINE FLOOR CHECK FAILED: deployed engine at {caps.base_url} reports "
            f"release_version {caps.release_version!r}, required floor is v{floor}.\n"
            f"{_REMEDY}",
            file=sys.stderr,
        )
        return 1

    print(
        f"cloud engine is current: {caps.base_url} release_version="
        f"{caps.release_version} (floor v{floor})",
    )
    return 0


#: engine tag (or the literal "next" for the tag about to be cut) -> the
#: client commits that must be in a RELEASED conexus version before that
#: engine may DEPLOY. Commits, not branches: a branch can move, a commit
#: either is or is not an ancestor of the release tag.
#:
#: Rows exist only for engines AHEAD of ``REQUIRED_ENGINE_VERSION`` -- once the
#: floor reaches a row's engine, the deploy it gated has happened and the row
#: is dead weight (:func:`stale_precondition_rows`). Pruned rows, for the record:
#:   engine-service-v0.1.61 / a62649ef (nexus-9ssih dangling-endpoint 400)
#:   engine-service-v0.1.62 / 9ba82a3b (nexus-lcmbp 409 conflict_running)
ENGINE_CLIENT_PRECONDITIONS: dict[str, dict[str, str]] = {}

_PRECONDITION_REMEDY = (
    "Remedy: this blocks the DEPLOY only -- the engine tag cuts whenever the "
    "tree is green (a tag gates delivery, not work). Pair the deploy with the "
    "conexus release that carries the listed commit(s) AND bumps the floor to "
    "this tag: deploy fires at client-tag push, in parallel with the PyPI "
    "publish (AGENTS.md § Engine-service release, paired-release "
    "choreography). Then re-run this check."
)


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def latest_release_tag() -> str:
    """The most recent conexus release tag (vX.Y.Z, not engine-service-*)."""
    tags = _git("tag", "-l", "v[0-9]*", "--sort=-v:refname").splitlines()
    if not tags:
        raise RuntimeError("no v* release tags found")
    return tags[0]


def is_ancestor(commit: str, tag: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, tag],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise RuntimeError(
        f"git merge-base --is-ancestor {commit} {tag}: {proc.stderr.strip()}"
    )


def check_client_precondition(engine_tag: str) -> int:
    """Are the client commits ``engine_tag`` requires already released?

    Gates the DEPLOY, never the tag cut. The DATA EFFECT relay is checked
    first, so a missing or stale attestation is never masked by an empty hand
    table (nexus-iu43o); then the hand table; then the wire-contract ledger,
    which is NOT tag-scoped -- a blocking ``## Unshipped`` entry means some
    client half is missing from every released version, so an unpaired deploy
    of ANY engine tag risks carrying that gap live.
    """
    relay_rc = check_data_effect_relay(engine_tag)
    if relay_rc != 0:
        return relay_rc

    required = ENGINE_CLIENT_PRECONDITIONS.get(engine_tag, {})
    if required:
        try:
            release = latest_release_tag()
        except RuntimeError as e:
            print(f"CANNOT VERIFY: {e}", file=sys.stderr)
            return 2
        missing = []
        for commit, why in required.items():
            try:
                ok = is_ancestor(commit, release)
            except RuntimeError as e:
                print(f"CANNOT VERIFY {commit}: {e}", file=sys.stderr)
                return 2
            status = "in" if ok else "MISSING FROM"
            print(f"  {commit}  {status} {release}  ({why.splitlines()[0]}...)")
            if not ok:
                missing.append((commit, why))
        if missing:
            commits = "\n".join(f"  {commit}: {why}" for commit, why in missing)
            print(
                "\n"
                f"BLOCKED: {engine_tag} must not deploy -- {len(missing)} required client "
                f"commit(s) absent from the latest release {release}:\n"
                f"{commits}\n"
                "\n"
                f"{_PRECONDITION_REMEDY}",
                file=sys.stderr,
            )
            return 1
        print(f"OK: all client preconditions for {engine_tag} are in {release}")

    ledger_vacuous = not _wire_ledger.parse_ledger(_wire_ledger.DEFAULT_LEDGER_PATH).unshipped
    ledger_rc = check_client_lag_ledger()
    if ledger_rc != 0:
        return ledger_rc
    if not required and ledger_vacuous:
        # An empty table is a LEGITIMATE state but indistinguishable from
        # "nobody filled the row in", so say out loud that nothing was verified
        # (nexus-f9z84).
        print(
            f"OK (VACUOUS -- 0 preconditions registered for {engine_tag} AND 0 entries "
            f"in {_wire_ledger.DEFAULT_LEDGER_PATH}'s ## Unshipped section): this run "
            "verified NOTHING from EITHER source, so it is not evidence the deploy "
            "is safe.",
        )
    return 0


def stale_precondition_rows(
    table: dict[str, dict[str, str]] | None = None,
    floor: tuple[int, ...] | None = None,
) -> list[str]:
    """Rows at or behind the floor -- dead weight per the table's contract.

    Takes an injectable table and floor so a test can plant a stale row and
    watch it come back; looping the real (empty) table proves nothing
    (nexus-f9z84).
    """
    rows = ENGINE_CLIENT_PRECONDITIONS if table is None else table
    active_floor = REQUIRED_ENGINE_VERSION if floor is None else floor
    stale: list[str] = []
    for tag in rows:
        if tag == "next":  # the about-to-be-cut sentinel is always ahead
            continue
        version = tuple(int(n) for n in tag.removeprefix("engine-service-v").split("."))
        if version <= active_floor:
            stale.append(tag)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Managed service base URL override. Defaults to the resolved "
        "managed endpoint (NX_SERVICE_URL / config.yml / "
        "https://api.conexus-nexus.com).",
    )
    parser.add_argument(
        "--paired-deploy",
        default=None,
        metavar="TAG",
        help="Paired-release mode (nexus-k1c08). NEVER the default. Accepts a "
        "cloud engine reported BELOW REQUIRED_ENGINE_VERSION as expected "
        "pre-deploy, PROVIDED TAG (engine-service-vX.Y.Z) independently "
        "verifies as published (non-draft GH release with the "
        f"{_REQUIRED_ASSET_NAME} asset), pins REQUIRED_ENGINE_VERSION "
        "exactly, is the newest published engine-service tag, AND was "
        "authored within the freshness window (see "
        "--paired-tag-max-age-hours). Post-tag, re-run WITHOUT this flag as "
        "the deploy-window VERIFY.",
    )
    parser.add_argument(
        "--paired-deploy-auto",
        action="store_true",
        help="Auto-paired mode (nexus-gc9ir). The unattended counterpart of "
        "--paired-deploy for release.yml: derives the tag from "
        "REQUIRED_ENGINE_VERSION and, ONLY when the cloud is confirmed "
        "below that floor, applies the IDENTICAL --paired-deploy "
        "verification battery to it. When the cloud already meets the "
        "floor this is a bare-invocation pass. Mutually exclusive with "
        "--paired-deploy.",
    )
    parser.add_argument(
        "--paired-tag-max-age-hours",
        type=float,
        default=_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS,
        metavar="HOURS",
        help="Only with --paired-deploy or --paired-deploy-auto: override the "
        f"paired-tag freshness window (default {_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS:g}h). "
        "Use only when this release genuinely lagged its engine tag -- the "
        "default stops a reused pairing from accepting a stale tag across "
        "multiple releases.",
    )
    parser.add_argument(
        "--ledger-only",
        action="store_true",
        help="Pre-tag mode (nexus-55r6o). Runs ONLY check_client_lag_ledger "
        "-- the tree-static docs/wire-contract-pending.md read, no network "
        "probe, no git-ancestry check -- and exits with its result. For "
        "release-branch PR CI. Mutually exclusive with --url, "
        "--paired-deploy, --paired-deploy-auto and --client-precondition.",
    )
    parser.add_argument(
        "--client-precondition",
        nargs="?",
        const="",
        default=None,
        metavar="TAG",
        help="Deploy-order mode (nexus-9ssih). Verify that the client commits "
        "engine tag TAG requires are in the latest released conexus version, "
        "and that the wire-contract ledger has no blocking entry. TAG "
        "defaults to the pinned REQUIRED_ENGINE_VERSION tag. Gates the "
        "DEPLOY, never the tag cut. Mutually exclusive with every other mode.",
    )
    args = parser.parse_args(argv)
    if args.paired_deploy is not None and args.paired_deploy_auto:
        parser.error("--paired-deploy and --paired-deploy-auto are mutually exclusive")
    if args.ledger_only and (
        args.paired_deploy is not None or args.paired_deploy_auto or args.url is not None
    ):
        parser.error(
            "--ledger-only is mutually exclusive with --url, "
            "--paired-deploy, and --paired-deploy-auto"
        )
    if args.client_precondition is not None:
        if (
            args.ledger_only
            or args.paired_deploy is not None
            or args.paired_deploy_auto
            or args.url is not None
        ):
            parser.error(
                "--client-precondition is mutually exclusive with --url, "
                "--paired-deploy, --paired-deploy-auto and --ledger-only"
            )
        return check_client_precondition(args.client_precondition or _pinned_engine_tag())
    if args.ledger_only:
        return check_client_lag_ledger()
    rc = check_floor(
        url=args.url,
        paired_deploy=args.paired_deploy,
        paired_deploy_auto=args.paired_deploy_auto,
        paired_tag_max_age_hours=args.paired_tag_max_age_hours,
    )
    if rc != 0:
        return rc
    # nexus-hs4xl: version agreement (just proven above) does not imply
    # source agreement. Compare against the SAME tag check_floor just
    # validated -- the paired tag when armed (which legitimately carries
    # service source destined for the parallel cut), the pinned floor's tag
    # otherwise.
    return check_source_ancestry(args.paired_deploy or _pinned_engine_tag())


if __name__ == "__main__":
    raise SystemExit(main())
