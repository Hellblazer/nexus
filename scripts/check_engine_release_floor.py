#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Blocking release-gate: is the LIVE deployed cloud engine current? (nexus-i5c2u)

Phase 4 of the engine-version-floor unification (parent bug nexus-b6qlf).
Root cause this closes: the release checklist's "Engine-freshness gate" step
was pure prose -- a human had to manually run
``git log <pinned-engine-tag>..HEAD -- service/`` and judge whether the drift
was "non-trivial AND cloud-relevant". That judgment call was skipped in
practice: the cloud engine sat at ``engine-service-v0.1.17`` for 9+ days
across multiple client releases while develop's
:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` floor moved to
``(0, 1, 34)``. This script replaces the eyeball check with a mechanical one:
probe the live managed service's ``/version`` handshake and compare its
``release_version`` against the floor. Exit non-zero (with the deployed
version, the required floor, and the remedy) when it is stale -- a release
runbook can then treat this as a hard prerequisite instead of an optional
step to skim past.

Reuses :func:`nexus.db.managed_endpoint.resolve_managed_endpoint` and
:func:`nexus.db.managed_endpoint.probe_managed_service` for all HTTP /
endpoint-resolution logic, and :func:`nexus.engine_version.parse_engine_version`
for all version-string parsing -- this module owns none of that, only the
floor comparison and CLI/exit-code wiring. Note that ``probe_managed_service``
itself already fails closed (raises :class:`ManagedServiceIncompatible`) on a
below-floor ``release_version``; the explicit comparison here is a second,
independently-testable layer so this gate does not silently pass if that
internal behavior ever changes, and so a caller sees the SAME "named versions"
message regardless of which layer caught the drift.

**Paired-release mode** (``--paired-deploy engine-service-vX.Y.Z``, nexus-k1c08,
2026-08-03): under the paired-release choreography (Hal directive 2026-08-02,
AGENTS.md § Cutting a release step 0), a client release bumps
``REQUIRED_ENGINE_VERSION`` to an engine tag whose deploy fires AT client-tag
push -- so PRE-tag, "cloud ``/version`` reports behind floor" is the EXPECTED
state, not the i5c2u/b6qlf 9-day-drift red this gate exists to catch. Before
this flag existed, a paired release's cloud-behind red had no way to
distinguish itself from real drift except a human writing "acknowledged
paired" prose into the release PR body -- exactly the eyeball-check failure
mode this whole module replaced. The flag is NEVER a default: it demands the
caller name the specific tag being paired against, and accepts a below-floor
cloud only when ALL of the following independently verify:

(a) the named tag exists in git AND is a PUBLISHED GitHub release --
    ``gh release view <tag> --json isDraft,assets`` reports non-draft with a
    ``nexus-service-linux-amd64`` asset present. That specific asset, not a
    bare non-empty check, because ``engine-service-release.yml`` documents
    (and its own comments accept) that both its asset-producing matrices run
    ``fail-fast: false``, so a release can be non-draft with real assets
    attached -- a PG bundle, sha256/cosign sidecars -- while carrying ZERO
    native binaries; ``nexus-service-linux-amd64`` is the specific artifact
    conexus deploy consumes (per the workflow's own release-notes comment), so
    its presence is what "published" needs to mean here. ``gh`` missing,
    failing to answer, or answering without a readable ``isDraft`` field is
    fail-closed -- same doctrine as :data:`_TAGS_UNAVAILABLE`: "cannot verify"
    is never "assume fine".
(b) ``REQUIRED_ENGINE_VERSION`` equals the tag's parsed version EXACTLY --
    the flag must name the tag THIS release actually pairs with, not any
    published engine tag.
(c) the named tag is the newest published ``engine-service-v*`` tag -- a
    newer tag than the pairing means unaccounted engine work, and the
    ordinary pin-currency red stays in force.
(d) the named tag's commit was authored within a bounded freshness window
    (default 72h, ``--paired-tag-max-age-hours`` to override) of "now". (a)-
    (c) are otherwise STABLE facts: once a pairing is armed they stay true
    indefinitely if no further engine tag is cut, so without a freshness
    bound an operator who reuses ``--paired-deploy`` on a LATER release (the
    promised post-tag VERIFY skipped, or the deploy silently failed) gets the
    identical acceptance forever -- reopening the exact i5c2u multi-release
    drift class this gate exists to close, now with a mechanically-approved
    green instead of a human eyeball. "Deploy fires AT client-tag push" is
    the choreography's own claim; a pairing that is weeks old is not this
    release's partner.

Any single miss keeps the gate red with a named reason -- paired mode makes
the acceptance MORE explicit than the default path, not looser. On an armed
pairing, a below-floor (or unparseable) cloud ``release_version`` is accepted
with an explicit "PAIRED MODE" acknowledgment naming the deployed and floor
versions and instructing the POST-TAG VERIFY: re-run this script WITHOUT
``--paired-deploy`` once the deploy lands, to confirm the cloud engine
actually converged rather than silently trusting the pairing forever. An
at-or-above-floor cloud still prints the ordinary current message (nothing to
acknowledge), and an unreachable cloud is still exit 2 regardless of pairing
-- unverifiable is never a pass, paired or not. The default (no flag) path is
byte-for-byte unchanged.

**Auto-paired mode** (``--paired-deploy-auto``, nexus-gc9ir, 2026-08-18): the
UNATTENDED counterpart of ``--paired-deploy`` for ``release.yml``'s own copy
of this gate. v7.10.0 tag push showed the gap: the workflow ran this script
bare, with no way to name ``--paired-deploy <tag>`` (there is no human at the
keyboard to type it), so a routine paired-release's EXPECTED pre-deploy
cloud-behind state red'd the publish and forced a deploy-first-then-
``gh run rerun`` dance that defeats the whole point of the parallel window.
``--paired-deploy-auto`` DERIVES the candidate tag from
``REQUIRED_ENGINE_VERSION`` (``engine-service-v{major}.{minor}.{patch}`` --
the same string :func:`_pinned_engine_tag` computes) and, ONLY when the cloud
actually reports below that floor, runs the IDENTICAL verification battery
:func:`check_paired_preconditions` already applies to the explicit flag --
one shared function, two entry points, so a miss in auto mode fails for the
exact same named reason an explicit ``--paired-deploy`` miss would. Auto mode
is NOT a weaker check: when the cloud already meets the floor it takes the
untouched bare-invocation path (pin-currency, then the ordinary "current"
message) -- the paired machinery (ledger, git/gh tag verification) never
runs, because the workflow probes the cloud FIRST to decide, rather than
demanding an explicit human-named tag up front the way ``--paired-deploy``
does. An unreachable cloud stays exit 2 regardless -- same fail-closed
doctrine as everywhere else in this module. On acceptance the printed
acknowledgment states the pairing was AUTO-derived (not human-named) in
addition to the usual deployed/floor versions and the POST-TAG VERIFY
obligation.

**The core tradeoff, stated plainly (review round, 2026-08-18):** BOTH paired
modes accept on TAG legitimacy (published, exactly pinned, newest, fresh),
never on proof that the deploy has actually landed. Passing this gate no
longer means "the deployed cloud engine meets the floor" for a paired
release -- it means "a genuinely fresh, correctly-cut engine tag exists, and
the deploy is presumed armed for the choreography's parallel window". A
CI-side check has no way to observe the deploy relay's actual completion
(it runs on a different system, on Hal's side of the AGENTS.md bus), so this
is an accepted, bounded gap, not an oversight: the pre-existing DAILY
``engine-floor-verify`` job (``.github/workflows/scheduled-failure-watch.yml``,
09:23 UTC, BARE gate against the real public endpoint, protocol-audit
[22511] Gap 2) is the backstop that would catch a still-stale cloud -- a
deploy that never fired, or silently failed -- within at most 24h of the
paired tag, surfaced as the SAME "Scheduled workflows are failing silently"
tracked GH issue every other rotted scheduled gate reports through (see that
workflow's header comment). This gate accepting a paired tag is therefore a
DELIBERATE, backstopped bet, not a claim that the deploy is verified live;
see ``test_auto_paired_below_floor_accepts_without_deploy_liveness_signal_by_design``
in the test file for the behavior pin.

Relatedly: paired acceptance (both modes) requires a GENUINE, parseable
below-floor ``release_version`` reading -- never merely "the probe raised
some ``ManagedServiceError``". ``probe_managed_service`` raises
:class:`ManagedServiceIncompatible` for five distinct reasons (non-200,
non-JSON body, missing/unparseable ``release_version``, AND the one that
matters here, a parseable version below the floor) and only the last one
populates the exception's structured ``deployed_version`` field. An endpoint
that is simply broken or misconfigured is NOT "deploy pending" and must
never be folded into paired acceptance -- see :func:`_classify_probe_failure`.

Usage::

    uv run python scripts/check_engine_release_floor.py
    uv run python scripts/check_engine_release_floor.py --url https://staging.example.com
    uv run python scripts/check_engine_release_floor.py --paired-deploy engine-service-v0.1.63
    uv run python scripts/check_engine_release_floor.py --paired-deploy-auto

Exit codes: ``0`` current, ``1`` stale / incompatible, ``2`` unreachable
(network/DNS/TLS/timeout -- "could not verify" is never treated as "must be
fine").
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import check_wire_contract_pairing as _wire_ledger
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
            "Cannot verify that every gated engine tag is pinned -- treat as a "
            "failed gate, not a pass. In CI, actions/checkout needs `fetch-tags: true`.",
            file=sys.stderr,
        )
        return 2
    if newest is None:
        print(
            "ENGINE PIN CHECK FAILED: zero engine-service-v* tags visible. Either "
            "the checkout has no tags (CI: set `fetch-tags: true`) or the tag "
            "namespace changed. A gate that sees nothing must not report success.",
            file=sys.stderr,
        )
        return 2
    if newest > REQUIRED_ENGINE_VERSION:
        newest_s = ".".join(str(p) for p in newest)
        print(
            f"ENGINE PIN CHECK FAILED: engine-service-v{newest_s} is published but this "
            f"release pins v{floor}. Local-mode installs receive ONLY the pinned "
            f"identity, so every engine fix between v{floor} and v{newest_s} reaches "
            f"nobody.\n{_UNPINNED_REMEDY}",
            file=sys.stderr,
        )
        return 1
    print(f"engine pin is current: REQUIRED_ENGINE_VERSION v{floor} == newest published tag")
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
            f"ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: could not confirm "
            f"{pinned_tag} exists in git. Cannot compare source trees -- "
            "treat as a failed gate, not a pass. In CI, actions/checkout "
            "needs `fetch-depth: 0` (release.yml already sets this).",
            file=sys.stderr,
        )
        return 2
    if not exists:
        print(
            f"ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: {pinned_tag} does not "
            "exist in this checkout's git history. Cannot compare source "
            "trees -- treat as a failed gate, not a pass.",
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
            f"ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: "
            f"`git diff {pinned_tag} HEAD -- {_ANCESTRY_SCOPE}` exited "
            f"{out.returncode}: {out.stderr.strip()}",
            file=sys.stderr,
        )
        return 2
    diff = out.stdout.strip()
    if diff:
        print(
            f"ENGINE SOURCE-ANCESTRY CHECK FAILED: this release ships "
            f"{_ANCESTRY_SCOPE} source that its pinned engine tag "
            f"({pinned_tag}) does not contain:\n{diff}\n"
            "The floor is version-CURRENT but SOURCE-STALE: a pin equal to "
            "the newest published tag can still predate shipped engine "
            "source (nexus-ajlz5). Cut a fresh engine tag carrying this "
            "source (or re-pin to a tag that already does) before "
            "releasing -- see AGENTS.md § Engine-service release, "
            "paired-release choreography.",
            file=sys.stderr,
        )
        return 1
    print(
        f"engine source is current: no {_ANCESTRY_SCOPE} drift between "
        f"{pinned_tag} and HEAD"
    )
    return 0


def check_client_lag_ledger(ack_beads: list[str] | None = None) -> int:
    """The both-halves wire-contract ledger gate for the paired-deploy path
    (nexus-1vogq). The engine-side complement to
    ``scripts/check_wire_contract_pairing.py``'s static tripwire: this is
    where the DEPLOY relay itself surfaces an unshipped client half BY NAME,
    rather than relying on someone having read the ledger prose.

    A non-empty ``## Unshipped`` section blocks paired-deploy UNLESS every
    entry's bead is named via ``--ack-client-lag <bead-id>`` -- an explicit
    "yes, I know this client half is not out yet, deploy anyway" rather than
    a silent pass. ``ack_beads=None`` behaves like an empty list: no
    acknowledgment offered.

    Returns ``0`` (ledger empty, or every entry acknowledged) or ``1``
    (unacknowledged entries present -- named in the message).
    """
    ledger = _wire_ledger.parse_ledger(_wire_ledger.DEFAULT_LEDGER_PATH)
    if not ledger.unshipped:
        print(
            f"client-lag ledger clean: 0 unshipped both-halves commits in "
            f"{_wire_ledger.DEFAULT_LEDGER_PATH}"
        )
        return 0

    acked = set(ack_beads or [])
    missing = [e for e in ledger.unshipped.values() if e.bead not in acked]
    if missing:
        print(
            f"PAIRED DEPLOY BLOCKED: {len(missing)} both-halves commit(s) in "
            f"{_wire_ledger.DEFAULT_LEDGER_PATH} have an unshipped client half "
            "and no acknowledgment:",
            file=sys.stderr,
        )
        for e in missing:
            print(
                f"  {e.sha}  bead {e.bead}  engine tag {e.engine_tag}  ({e.note})",
                file=sys.stderr,
            )
        print(
            "\nThis engine tag cannot deploy without an explicit paired-client "
            "acknowledgment (nexus-1vogq). Either pair this deploy with the "
            "client release carrying the listed commit(s), or pass "
            "--ack-client-lag <bead-id> for each entry above to deploy "
            "anyway.",
            file=sys.stderr,
        )
        return 1

    print(
        f"client-lag ledger: {len(ledger.unshipped)} unshipped both-halves "
        f"commit(s), all explicitly acknowledged via --ack-client-lag: "
        f"{', '.join(sorted(acked & {e.bead for e in ledger.unshipped.values()}))}"
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
            "exists. Cannot verify the pairing -- treat as a failed gate, not a "
            "pass.",
            file=sys.stderr,
        )
        return 2
    if not exists:
        print(
            f"PAIRED MODE REJECTED: {tag} does not exist in git. --paired-deploy "
            "must name a tag that has actually been pushed.",
            file=sys.stderr,
        )
        return 1

    published, reason = _paired_tag_published(tag)
    if published is _TAGS_UNAVAILABLE:
        print(
            f"PAIRED MODE UNVERIFIABLE: {reason}. Cannot verify publication -- "
            "treat as a failed gate, not a pass.",
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
            f"REQUIRED_ENGINE_VERSION is v{floor} -- wrong pairing. The flag must "
            "name the exact tag this release pairs with.",
            file=sys.stderr,
        )
        return 1

    if newest is _TAGS_UNAVAILABLE:
        print(
            "PAIRED MODE UNVERIFIABLE: could not read engine-service tags from "
            "git to confirm no newer tag exists.",
            file=sys.stderr,
        )
        return 2
    if newest is None or newest != parsed_tag:
        newest_s = ".".join(str(p) for p in newest) if newest is not None else "none"
        print(
            f"PAIRED MODE REJECTED: newest published engine tag is v{newest_s}, "
            f"not v{tag_s} -- a newer engine tag exists than the one this release "
            "pairs with; unaccounted engine work. Keep the pin-currency red until "
            "it is pinned or explained.",
            file=sys.stderr,
        )
        return 1

    age_hours = _tag_age_hours(tag)
    if age_hours is _TAGS_UNAVAILABLE:
        print(
            f"PAIRED MODE UNVERIFIABLE: could not determine {tag}'s commit age "
            "from git. Cannot verify the pairing is fresh -- treat as a failed "
            "gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    if age_hours > max_age_hours:
        print(
            f"PAIRED MODE REJECTED: {tag} is {age_hours:.1f}h old, past the "
            f"{max_age_hours:.1f}h paired-tag freshness window. Deploy fires AT "
            f"client-tag push -- a pairing this old is not THIS release's "
            "partner, and accepting it reopens the multi-release i5c2u drift "
            "class this gate exists to close. If this release genuinely lagged "
            "its engine tag, override explicitly with "
            "--paired-tag-max-age-hours.",
            file=sys.stderr,
        )
        return 1

    print(
        f"paired mode ARMED: {tag} verified published, pinned to "
        f"REQUIRED_ENGINE_VERSION, newest published engine tag, and "
        f"{age_hours:.1f}h old (within the {max_age_hours:.1f}h window)."
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


def _probe_unverifiable_message(base: str, floor: str, detail: str) -> str:
    """Shared stderr text for a probe result paired mode refuses to accept
    as "deploy pending" -- an endpoint error or a malformed/unparseable
    response, as distinct from a genuine parseable below-floor version
    (see :func:`_classify_probe_failure`).
    """
    return (
        f"ENGINE FLOOR CHECK UNVERIFIABLE (required v{floor}): managed "
        f"service at {base} {detail}. Paired mode only ever accepts a "
        "GENUINE, parseable below-floor version report as 'deploy pending' "
        "-- an endpoint error or a malformed/unparseable response is never "
        "folded into that acceptance, paired or not. Treat as a failed "
        "gate, not a pass."
    )


def _print_paired_ack(deployed: str, floor: str, auto: bool = False) -> None:
    """Explicit acknowledgment for an accepted paired-mode cloud-behind result.

    Names both versions (never a bare "OK") and states the POST-TAG VERIFY
    obligation up front -- a paired acceptance is a deferred check, not a
    closed one, and the caller must re-run without ``--paired-deploy`` once
    the deploy lands to confirm the pairing actually converged.

    ``auto=True`` (nexus-gc9ir): the pairing was DERIVED from
    ``REQUIRED_ENGINE_VERSION`` by ``--paired-deploy-auto``, not named by a
    human via ``--paired-deploy`` -- the acknowledgment says so explicitly so
    a reader of CI logs can tell the two modes apart.
    """
    origin = (
        "AUTO-derived from REQUIRED_ENGINE_VERSION (--paired-deploy-auto, "
        "nexus-gc9ir) -- no explicit --paired-deploy given."
        if auto
        else "named via --paired-deploy."
    )
    print(
        f"PAIRED MODE: cloud reports release_version {deployed!r}, behind floor "
        f"v{floor}. Expected pre-deploy under the paired-release choreography -- "
        "the deploy fires at client-tag push (AGENTS.md § Cutting a "
        f"release, step 0), not before this tag exists. Pairing {origin}\n"
        "POST-TAG VERIFY REQUIRED: re-run this script WITHOUT --paired-deploy "
        "once the deploy lands, to confirm the cloud engine actually converged "
        "-- escalate loudly (never silently re-accept) if it is still behind at "
        "that point."
    )


def _run_paired_precondition_battery(
    tag: str,
    newest: object,
    paired_tag_max_age_hours: float,
    ack_client_lag: list[str] | None,
) -> int:
    """The local, no-network battery an ARMED pairing must clear -- shared by
    BOTH explicit ``--paired-deploy`` and auto-derived ``--paired-deploy-auto``
    (nexus-gc9ir review round: this sequence was duplicated between
    :func:`check_floor`'s explicit branch and the auto-mode tail, which is
    exactly the drift-prone-release-machinery class this bead exists to
    close -- one function, two call sites, so a future change to the
    battery cannot land in only one mode by accident).

    Order: the both-halves wire-contract ledger (nexus-1vogq) FIRST -- local,
    no network, actionable without ever looking at ``tag``'s git/gh state --
    THEN :func:`check_paired_preconditions` (nexus-k1c08).

    Returns 0 when both pass; the first failing check's own named-reason
    exit code otherwise.
    """
    ledger_rc = check_client_lag_ledger(ack_client_lag)
    if ledger_rc != 0:
        return ledger_rc
    return check_paired_preconditions(tag, newest, max_age_hours=paired_tag_max_age_hours)


def _paired_below_floor_path(
    deployed_version: str,
    newest: object,
    paired_tag_max_age_hours: float,
    ack_client_lag: list[str] | None,
) -> int:
    """Shared tail of auto-paired mode once the cloud is confirmed below floor.

    Runs :func:`_run_paired_precondition_battery` -- the IDENTICAL local
    battery :func:`check_floor` runs for an explicit ``--paired-deploy`` --
    on the tag AUTO-derived from ``REQUIRED_ENGINE_VERSION``. On success,
    prints the paired acknowledgment with ``auto=True`` and returns 0; any
    precondition miss returns its own named-reason code unchanged.
    """
    tag = _pinned_engine_tag()
    paired_rc = _run_paired_precondition_battery(
        tag, newest, paired_tag_max_age_hours, ack_client_lag
    )
    if paired_rc != 0:
        return paired_rc
    floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    _print_paired_ack(deployed_version, floor, auto=True)
    return 0


def _check_floor_auto_paired(
    url: str | None,
    newest: object,
    paired_tag_max_age_hours: float,
    ack_client_lag: list[str] | None,
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
            f"ENGINE FLOOR CHECK FAILED: managed service at {base} is unreachable "
            f"({exc}). Cannot verify the cloud engine version -- treat this as a "
            "failed gate, not a pass.",
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
                _probe_unverifiable_message(
                    base, floor,
                    f"probe failed without a genuine below-floor version reading ({exc})",
                ),
                file=sys.stderr,
            )
            return 2
        return _paired_below_floor_path(deployed, newest, paired_tag_max_age_hours, ack_client_lag)

    parsed = parse_engine_version(caps.release_version)
    if parsed is not None and parsed >= REQUIRED_ENGINE_VERSION:
        # Cloud already meets the floor: EXACTLY the bare-invocation path.
        pin_rc = check_pin_currency(newest)
        if pin_rc != 0:
            return pin_rc
        print(
            f"cloud engine is current: {caps.base_url} release_version="
            f"{caps.release_version} (floor v{floor})"
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
            _probe_unverifiable_message(
                base, floor,
                f"reported an unparseable release_version {caps.release_version!r}",
            ),
            file=sys.stderr,
        )
        return 2

    # Reachable, with a genuine parseable release_version below the floor.
    return _paired_below_floor_path(
        caps.release_version, newest, paired_tag_max_age_hours, ack_client_lag
    )


def check_floor(
    url: str | None = None,
    newest: object | None = None,
    paired_deploy: str | None = None,
    paired_deploy_auto: bool = False,
    paired_tag_max_age_hours: float = _DEFAULT_PAIRED_TAG_MAX_AGE_HOURS,
    ack_client_lag: list[str] | None = None,
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
            ack_client_lag=ack_client_lag,
        )

    if paired_deploy is not None:
        # _run_paired_precondition_battery: ledger (nexus-1vogq) FIRST -- local,
        # no network, same "cheap local check before anything else" ordering as
        # pin-currency below -- THEN check_paired_preconditions (nexus-k1c08).
        # Shared with auto mode's tail (nexus-gc9ir) so the two entry points
        # can never drift on what "armed" means.
        paired_rc = _run_paired_precondition_battery(
            paired_deploy, resolved_newest, paired_tag_max_age_hours, ack_client_lag
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
            f"ENGINE FLOOR CHECK FAILED: managed service at {base} is unreachable "
            f"({exc}). Cannot verify the cloud engine version -- treat this as a "
            "failed gate, not a pass.",
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
                    _probe_unverifiable_message(
                        base, floor,
                        f"probe failed without a genuine below-floor version reading ({exc})",
                    ),
                    file=sys.stderr,
                )
                return 2
            _print_paired_ack(deployed, floor)
            return 0
        print(
            f"ENGINE FLOOR CHECK FAILED (required v{floor}): {exc}\n{_REMEDY}",
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
                    _probe_unverifiable_message(
                        base, floor,
                        f"reported an unparseable release_version {caps.release_version!r}",
                    ),
                    file=sys.stderr,
                )
                return 2
            _print_paired_ack(caps.release_version, floor)
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
        f"{caps.release_version} (floor v{floor})"
    )
    return 0


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
        "--paired-deploy for release.yml's own copy of this gate, where "
        "there is no human to name a tag. Derives the candidate tag from "
        "REQUIRED_ENGINE_VERSION and, ONLY when the cloud is confirmed "
        "below that floor, applies the IDENTICAL --paired-deploy "
        "verification battery to it. When the cloud already meets the "
        "floor this is a byte-for-byte bare-invocation pass -- never "
        "weaker than the default path. Mutually exclusive with "
        "--paired-deploy.",
    )
    parser.add_argument(
        "--paired-tag-max-age-hours",
        type=float,
        default=_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS,
        metavar="HOURS",
        help="Only with --paired-deploy or --paired-deploy-auto (nexus-gc9ir: "
        "the auto-derived tag goes through the IDENTICAL freshness check): "
        "override the paired-tag freshness window (default "
        f"{_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS:g}h). Use only when this release "
        "genuinely lagged its engine tag -- the default exists to stop a "
        "reused pairing from silently accepting a stale tag across multiple "
        "releases (nexus-k1c08 fix round).",
    )
    parser.add_argument(
        "--ack-client-lag",
        action="append",
        default=None,
        metavar="BEAD-ID",
        help="Only with --paired-deploy or --paired-deploy-auto (nexus-1vogq; "
        "auto mode's below-floor path runs the identical ledger check, "
        "nexus-gc9ir). Explicit acknowledgment that a both-halves commit's "
        "client half is not yet in a published release -- names the OWNING "
        "BEAD of a docs/wire-contract-pending.md ## Unshipped entry. Repeat "
        "for each entry. Without this, a non-empty ledger blocks the deploy "
        "-- release.yml's own auto invocation has no way to supply this flag "
        "(see its step comment for the operator remedy).",
    )
    args = parser.parse_args(argv)
    if args.paired_deploy is not None and args.paired_deploy_auto:
        parser.error("--paired-deploy and --paired-deploy-auto are mutually exclusive")
    rc = check_floor(
        url=args.url,
        paired_deploy=args.paired_deploy,
        paired_deploy_auto=args.paired_deploy_auto,
        paired_tag_max_age_hours=args.paired_tag_max_age_hours,
        ack_client_lag=args.ack_client_lag,
    )
    if rc != 0:
        return rc
    # nexus-hs4xl: version agreement (just proven above) does not imply
    # source agreement. Compare against the SAME tag check_floor just
    # validated -- the paired tag when armed (which legitimately carries
    # service source destined for the parallel cut), the pinned floor's tag
    # otherwise.
    ancestry_tag = args.paired_deploy or _pinned_engine_tag()
    return check_source_ancestry(ancestry_tag)


if __name__ == "__main__":
    raise SystemExit(main())
