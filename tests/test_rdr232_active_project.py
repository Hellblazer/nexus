# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 (nexus-jypmj.2.4, assumption A5): the active-project tier of named-source resolution.

Tenant-free: the catalog is the strict fake of ``test_rdr232_primary_evidence`` (two registered
owners, ART 1.2 and Nexus 1.1, each holding an RDR-084), widened with the one extra call the tier
makes (``owner_for_repo``). The identity chain from a project directory to its owner is the real
one (``rdr_canonical.current_rdr_owner`` over ``_repo_identity``); only the directory is a tmp path.

The resolution order, and what each test below holds:

1. an explicit ``owner``/``project`` binding, 2. the owner scope (a catalog-subtree ``scope``),
3. the active project, inferred, never narrowing the secondary streams, never guessed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nexus.catalog.tumbler import Tumbler
from nexus.mcp import core
from nexus.mcp.core import _nx_answer_owner_binding
from nexus.plans.active_project import (
    ActiveProject,
    apply_active_project,
    detect_active_project,
)
from nexus.plans.evidence_diagnostics import _owner_not_applied
from nexus.plans.evidence import (
    EvidenceRequest,
    OwnerNameError,
    OwnerResolution,
    SourceRef,
    build_evidence_request,
)
from nexus.plans.owner_narrowing import request_owners
from nexus.plans.primary_evidence import resolve_named_sources
from nexus.repo_identity import _repo_identity
from tests.test_rdr232_caller_scope import MULTI_STEP_PLAN, _drive_nx_answer, _match, _plan_run_result
from tests.test_rdr232_primary_evidence import (
    ART_DOC,
    ART_OWNER,
    ART_PATH,
    NEXUS_COL,
    NEXUS_DOC,
    NEXUS_OWNER,
    FakeCatalog,
    FakeT3,
    _doc,
    _two_projects,
)
from tests.test_rdr232_reserved_merge import _resolution_for_rdr084

RDR084 = SourceRef.parse("RDR-084")
NAMES = {ART_OWNER: "ART", NEXUS_OWNER: "nexus"}


class SessionCatalog(FakeCatalog):
    """The fake catalog plus ``owner_for_repo``: which owner a repository hash registered under."""

    def __init__(self, *args: Any, repo_hashes: Mapping[str, str] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.repo_hashes = dict(repo_hashes or {})
        self.fail_owner_lookup = False
        #: ``get_owner_by_prefix`` raises from this call on (1-based): 2 lets the detection's probe through and
        #: fails the resolver's own, which comes after it.
        self.fail_owner_probe_from: int | None = None
        self._probes = 0
        #: Registered owners with no documents, ``{tumbler: name}``: what ``list_owners`` adds to the two projects.
        self.extra_owners: dict[str, str] = {}

    def get_owner_by_prefix(self, prefix: str) -> dict | None:
        self._probes += 1
        if self.fail_owner_probe_from is not None and self._probes >= self.fail_owner_probe_from:
            self.calls.append(("get_owner_by_prefix", {"prefix": prefix}))
            raise RuntimeError("engine unreachable")
        return super().get_owner_by_prefix(prefix)

    def list_owners(self, *, include_deactivated: bool = False) -> list[dict]:
        rows = super().list_owners(include_deactivated=include_deactivated)
        return rows + [{"tumbler_prefix": t, "name": n, "owner_type": "repo"} for t, n in self.extra_owners.items()]

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        self.calls.append(("owner_for_repo", {"repo_hash": repo_hash}))
        if self.fail_owner_lookup:
            raise RuntimeError("engine unreachable")
        owner = self.repo_hashes.get(repo_hash)
        return Tumbler.parse(owner) if owner else None


def _session(tmp_path: Path, owner: str | None = ART_OWNER) -> tuple[SessionCatalog, FakeT3, dict[str, str], str]:
    """Two projects in the catalog, and a session whose project directory is *tmp_path*, registered
    under *owner* (``None``: never indexed). Returns ``(catalog, t3, environ, cwd)``."""
    project = tmp_path / "project"
    project.mkdir()
    _, repo_hash = _repo_identity(project)
    cat, t3 = SessionCatalog(repo_hashes={repo_hash: owner} if owner else {}), FakeT3()
    _two_projects(cat, t3)
    return cat, t3, {"CLAUDE_PROJECT_DIR": str(project)}, str(project)


def _detect(cat: SessionCatalog, environ: Mapping[str, str], cwd: str) -> ActiveProject:
    return detect_active_project(cat, environ=environ, getcwd=lambda: cwd)


def _request(bindings: dict[str, Any] | None = None, scope: str = "") -> EvidenceRequest:
    def lookup(name: str) -> str:
        for tumbler, owner_name in NAMES.items():
            if name == owner_name:
                return tumbler
        raise OwnerNameError(f"{name} names no owner")

    return build_evidence_request({"source": "RDR-084", **(bindings or {})}, scope=scope, name_lookup=lookup)


def _with_active_project(request: EvidenceRequest, active: ActiveProject, **kw: Any) -> EvidenceRequest:
    kw.setdefault("scope", "")
    tier = apply_active_project(request.owner, active, **kw)
    return replace(request, owner=tier.owner, active_project=tier.to_wire())


# ── the bead's three tests ───────────────────────────────────────────────────────────────────────────────


def test_the_unmodified_art_call_with_the_sessions_project_art_resolves_art_rdr_084(tmp_path: Path) -> None:
    """Bead test 1. The call names RDR-084 and no project (scope is the corpus ``rdr``); the session's
    project is ART, so ART's document resolves, and it says the project was inferred."""
    cat, _t3, environ, cwd = _session(tmp_path, ART_OWNER)
    active = _detect(cat, environ, cwd)
    assert (active.reliable, active.owner, active.name) == (True, ART_OWNER, "ART"), active.reason

    request = _with_active_project(_request(scope="rdr"), active, scope="rdr")
    assert request.owner.status == "inferred" and request.owner.tumbler == ART_OWNER

    (src,) = resolve_named_sources(request.sources, request.owner, cat=cat).sources
    assert (src.status, src.tumbler, src.inferred) == ("resolved", ART_DOC, True)
    assert NEXUS_DOC not in {c.tumbler for c in src.candidates}, "the other project's RDR-084 is not a candidate"
    resolution = resolve_named_sources(request.sources, request.owner, cat=cat)
    assert any("active project" in line and ART_DOC in line and "inferred" in line for line in resolution.disclosures())


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("no_project_dir_declared", "is unset"),
        ("declared_dir_missing", "does not exist"),
        ("cwd_is_not_the_declared_dir", "not the declared project directory"),
        ("cwd_unreadable", "cannot be read"),
        ("repo_never_indexed", "no catalog owner"),
        ("catalog_down", "failed (RuntimeError)"),
        ("owner_not_in_catalog", "is not in the catalog"),
    ],
)
def test_the_same_call_with_no_reliable_active_project_is_an_ambiguity_naming_both_documents(
    tmp_path: Path, case: str, why: str,
) -> None:
    """Bead test 2, once per way the project can fail to be reliable. Nothing is picked."""
    cat, _t3, environ, cwd = _session(tmp_path, None if case == "repo_never_indexed" else ART_OWNER)
    getcwd = lambda: cwd  # noqa: E731
    if case == "no_project_dir_declared":
        environ = {}
    elif case == "declared_dir_missing":
        environ = {"CLAUDE_PROJECT_DIR": str(tmp_path / "gone")}
    elif case == "cwd_is_not_the_declared_dir":
        other = tmp_path / "other"
        other.mkdir()
        getcwd = lambda: str(other)  # noqa: E731
    elif case == "cwd_unreadable":
        def getcwd() -> str:
            raise FileNotFoundError("deleted")
    elif case == "catalog_down":
        cat.fail_owner_lookup = True
    elif case == "owner_not_in_catalog":
        cat.owners.discard(ART_OWNER)
    active = detect_active_project(cat, environ=environ, getcwd=getcwd)
    assert active.reliable is False and not active.owner
    assert why in active.reason, active.reason

    request = _with_active_project(_request(scope="rdr"), active, scope="rdr")
    assert request.owner.status == "none", "an unreliable active project is not applied"
    assert request.active_project is not None and request.active_project["applied"] is False
    assert request.active_project["reason"] == active.reason

    resolution = resolve_named_sources(request.sources, request.owner, cat=cat)
    (src,) = resolution.sources
    assert src.status == "candidates" and src.tumbler is None
    assert sorted(c.tumbler for c in src.candidates) == [NEXUS_DOC, ART_DOC]
    assert any(ART_DOC in line and NEXUS_DOC in line for line in resolution.disclosures())


@pytest.mark.parametrize(
    ("bindings", "scope", "active_owner", "want"),
    [
        pytest.param({"owner": NEXUS_OWNER}, "rdr", ART_OWNER, NEXUS_DOC, id="owner-binding-beats-the-active-project"),
        pytest.param({"project": "nexus"}, "", ART_OWNER, NEXUS_DOC, id="project-name-beats-the-active-project"),
        pytest.param({"owner": ART_OWNER}, "", NEXUS_OWNER, ART_DOC, id="the-other-way-round"),
        pytest.param({}, NEXUS_OWNER, ART_OWNER, NEXUS_DOC, id="owner-scope-beats-the-active-project"),
        pytest.param({}, f"{NEXUS_OWNER}.3996", ART_OWNER, NEXUS_DOC, id="document-scope-beats-the-active-project"),
    ],
)
def test_an_explicit_owner_or_project_binding_outranks_the_active_project(
    tmp_path: Path, bindings: dict[str, Any], scope: str, active_owner: str, want: str,
) -> None:
    """Bead test 3, with the owner scope tier between the binding and the active project."""
    cat, _t3, environ, cwd = _session(tmp_path, active_owner)
    active = _detect(cat, environ, cwd)
    assert active.reliable and active.owner == active_owner

    request = _with_active_project(_request(bindings, scope), active, scope=scope)
    assert request.owner.status == "resolved", "the explicit tier already resolved; the active project is not consulted"
    assert "active_project" not in dict(request.owner.contributions)
    assert request.active_project is None or request.active_project["applied"] is False

    (src,) = resolve_named_sources(request.sources, request.owner, cat=cat).sources
    assert (src.status, src.tumbler, src.inferred) == ("resolved", want, False)


# ── what "reliable" means ────────────────────────────────────────────────────────────────────────────────


def test_a_reliable_active_project_is_declared_by_the_host_agreed_by_the_process_and_registered_in_the_catalog(
    tmp_path: Path,
) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    active = _detect(cat, environ, cwd)
    assert active.reliable and (active.owner, active.name) == (ART_OWNER, "ART")
    wire = active.to_wire()
    assert wire["reliable"] is True and wire["owner"] == ART_OWNER
    assert cwd not in str(wire), "the envelope never carries a filesystem path"


def test_the_declared_and_working_directories_agree_through_symlinks_and_trailing_separators(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(cwd)
    assert detect_active_project(cat, environ={"CLAUDE_PROJECT_DIR": str(link) + os.sep}, getcwd=lambda: cwd).reliable


def test_a_project_directory_that_is_not_a_catalog_owner_is_not_reliable_and_names_why(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path, None)
    active = _detect(cat, environ, cwd)
    assert not active.reliable and "no catalog owner" in active.reason
    assert cat.count("find") == 0 and cat.count("get_owner_by_prefix") == 0, "no owner, so nothing else is asked"


def test_a_relative_declared_directory_is_not_reliable(tmp_path: Path) -> None:
    cat, _t3, _environ, cwd = _session(tmp_path)
    active = detect_active_project(cat, environ={"CLAUDE_PROJECT_DIR": "project"}, getcwd=lambda: cwd)
    assert not active.reliable and "absolute" in active.reason


@pytest.mark.parametrize("junk", ["<MagicMock id=1>", "1", "1.2.3", "ART"])
def test_an_owner_that_is_not_an_owner_tumbler_is_not_reliable(tmp_path: Path, junk: str) -> None:
    """A catalog (or a test double) that answers with something else never becomes a qualifier."""
    cat, _t3, environ, cwd = _session(tmp_path)
    active = detect_active_project(
        cat, environ=environ, getcwd=lambda: cwd, owner_of=lambda _c, _r: junk,
    )
    assert not active.reliable and not active.owner and "not an owner tumbler" in active.reason
    forced = apply_active_project(OwnerResolution("none"), ActiveProject(True, owner=junk, name="x"), scope="")
    assert forced.applied is False and forced.owner.status == "none"


def test_detection_never_raises_whatever_the_catalog_does(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)

    class Broken:
        def owner_for_repo(self, repo_hash: str) -> Any:
            raise ValueError("boom")

    active = detect_active_project(Broken(), environ=environ, getcwd=lambda: cwd)
    assert not active.reliable and "ValueError" in active.reason

    def raising(_cat: Any, _root: Any) -> Any:
        raise OSError("git vanished")

    assert not detect_active_project(cat, environ=environ, getcwd=lambda: cwd, owner_of=raising).reliable
    assert not detect_active_project(None, environ=environ, getcwd=lambda: cwd).reliable


# ── scope="rdr" alone never selects a project ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["rdr", "code", "docs", "knowledge", "all", "docs__1-2__bge-base-en-v15-768__v1"])
def test_a_corpus_scope_alone_never_selects_a_project(tmp_path: Path, scope: str) -> None:
    cat, _t3, _environ, _cwd = _session(tmp_path)
    request = _request(scope=scope)
    assert request.owner.status == "none", "a corpus scope names no project"
    unreliable = ActiveProject(False, reason="no project directory was declared")
    tier = apply_active_project(request.owner, unreliable, scope=scope)
    assert tier.owner.status == "none" and tier.applied is False
    (src,) = resolve_named_sources(request.sources, tier.owner, cat=cat).sources
    assert src.status == "candidates" and src.tumbler is None


def test_with_a_reliable_active_project_a_corpus_scope_still_contributes_nothing(tmp_path: Path) -> None:
    """The project comes from the session, never from ``scope="rdr"``: the contribution says so."""
    cat, _t3, environ, cwd = _session(tmp_path)
    tier = apply_active_project(_request(scope="rdr").owner, _detect(cat, environ, cwd), scope="rdr")
    assert tier.owner.status == "inferred"
    assert dict(tier.owner.contributions) == {"active_project": ART_OWNER}, "no scope contribution"


def test_a_catalog_subtree_scope_is_the_owner_scope_tier_and_the_active_project_is_not_consulted(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    active = _detect(cat, environ, cwd)
    for scope in (NEXUS_OWNER, "1", f"{NEXUS_OWNER}.3996"):
        tier = apply_active_project(_request(scope=scope).owner, active, scope=scope)
        assert tier.applied is False and tier.owner.status != "inferred", scope


# ── an inferred owner never narrows the secondary streams ────────────────────────────────────────────────


def test_an_inferred_owner_never_narrows_the_secondary_streams(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    request = _with_active_project(_request(scope="rdr"), _detect(cat, environ, cwd), scope="rdr")
    assert request.owner.status == "inferred"
    assert request_owners(request) == (), "the 1.18 rule: explicit bindings only"
    assert _nx_answer_owner_binding(request) == {}
    # An explicit owner next to the same session still narrows, to the explicit one alone.
    explicit = _with_active_project(_request({"owner": NEXUS_OWNER}), _detect(cat, environ, cwd))
    assert request_owners(explicit) == (NEXUS_OWNER,) and _nx_answer_owner_binding(explicit) == {"_nx_owners": NEXUS_OWNER}


def test_the_diagnostics_do_not_report_an_inferred_owner_as_narrowing(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    request = _with_active_project(_request(), _detect(cat, environ, cwd))
    applied, not_applied = _owner_not_applied(request.owner.to_wire())
    assert applied is False and not_applied is None


def test_nx_answer_runs_an_inferred_request_without_the_owners_binding(tmp_path: Path) -> None:
    """End to end through ``nx_answer``: the active project is inferred for a ``source``-only call, reaches
    named-source resolution as the qualifier, and ``_nx_owners`` never reaches ``plan_run``."""
    cat, _t3, environ, cwd = _session(tmp_path)
    active = _detect(cat, environ, cwd)
    steps = [{"ids": ["c1"], "tumblers": ["1.2.1"]}, {"text": "The decision is X [1] after review of the evidence."}]
    run = AsyncMock(return_value=_plan_run_result(list(steps), []))
    resolution = _resolution_for_rdr084()
    resolution = replace(resolution, sources=tuple(replace(src, inferred=True) for src in resolution.sources))
    named = AsyncMock(return_value=resolution)
    with patch("nexus.plans.active_project.detect_active_project", return_value=active), \
         patch("nexus.mcp.core._nx_answer_named_sources", named), \
         patch("nexus.plans.runner.plan_run", run):
        env, _ = _drive_nx_answer(
            _match(MULTI_STEP_PLAN, default_bindings={"intent": "q"}), scope="rdr", structured=True,
            bindings={"source": "RDR-084"},
        )
    (resolved_request,) = named.await_args.args
    assert (resolved_request.owner.status, resolved_request.owner.tumbler) == ("inferred", ART_OWNER)
    assert "_nx_owners" not in dict(run.await_args.args[1])
    wire = env["evidence_request"]
    assert wire["owner"]["status"] == "inferred" and wire["owner"]["contributions"] == {"active_project": ART_OWNER}
    assert wire["active_project"]["applied"] is True and wire["active_project"]["name"] == "ART"
    assert env["evidence_diagnostics"]["scope_check"]["owner_narrowing"]["applied"] is False


# ── the resolver honours the inferred status without guessing ────────────────────────────────────────────


def test_an_explicit_tumbler_source_is_not_held_to_an_inferred_project() -> None:
    """A document tumbler names exactly one document; the session's project must not reject it."""
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    inferred = OwnerResolution("inferred", ART_OWNER, (("active_project", ART_OWNER),))
    (src,) = resolve_named_sources([SourceRef.parse(NEXUS_DOC)], inferred, cat=cat).sources
    assert (src.status, src.tumbler) == ("resolved", NEXUS_DOC)
    explicit = OwnerResolution("resolved", ART_OWNER, (("owner", ART_OWNER),))
    (held,) = resolve_named_sources([SourceRef.parse(NEXUS_DOC)], explicit, cat=cat).sources
    assert held.status == "unresolved", "an explicit qualifier still rejects a document outside it"


def test_an_inferred_project_with_no_such_rdr_falls_back_and_says_so() -> None:
    """The session is in ART, the question names an RDR only Nexus holds. Pre-2.4 behaviour stands (a lone
    document resolves, unqualified) and the miss is disclosed rather than turned into an error."""
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    _doc(cat, t3, "1.1.4000", "docs/rdr/rdr-085-other.md", ["# other"], collection=NEXUS_COL)
    inferred = OwnerResolution("inferred", ART_OWNER, (("active_project", ART_OWNER),))
    resolution = resolve_named_sources([SourceRef.parse("RDR-085")], inferred, cat=cat)
    (src,) = resolution.sources
    assert (src.status, src.tumbler, src.inferred) == ("resolved", "1.1.4000", False)
    assert "active project" in src.detail and ART_OWNER in src.detail
    assert any("active project" in line for line in resolution.disclosures())


def test_an_inferred_project_that_does_not_exist_in_the_catalog_degrades_to_no_qualifier() -> None:
    cat, t3 = FakeCatalog(owners=(NEXUS_OWNER,)), FakeT3()
    _two_projects(cat, t3)
    inferred = OwnerResolution("inferred", ART_OWNER, (("active_project", ART_OWNER),))
    (src,) = resolve_named_sources([RDR084], inferred, cat=cat).sources
    assert src.status == "candidates" and sorted(c.tumbler for c in src.candidates) == [NEXUS_DOC, ART_DOC]


def test_sibling_files_under_the_inferred_project_still_need_the_frontmatter_id() -> None:
    """The inferred qualifier narrows to a project; it does not choose among that project's sibling files."""
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", ART_PATH, ["# a"])
    _doc(cat, t3, "1.2.11", "docs/rdr/RDR-084-phase-notes.md", ["# b"])
    inferred = OwnerResolution("inferred", ART_OWNER, (("active_project", ART_OWNER),))
    (src,) = resolve_named_sources([RDR084], inferred, cat=cat).sources
    assert src.status == "candidates" and src.inferred is True
    assert "under 1.2" in src.detail and "active project" in src.detail


# ── the text may name a different project ────────────────────────────────────────────────────────────────


def test_a_project_the_question_names_that_is_not_the_active_one_withholds_the_inference(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path, NEXUS_OWNER)
    active = _detect(cat, environ, cwd)
    assert active.reliable and active.name == "nexus"
    tier = apply_active_project(OwnerResolution("none"), active, scope="", project_names=("ART",))
    assert tier.owner.status == "none" and tier.applied is False
    assert "ART" in tier.reason and "nexus" in tier.reason
    assert apply_active_project(OwnerResolution("none"), active, scope="", project_names=("NEXUS",)).applied is True
    assert apply_active_project(OwnerResolution("none"), active, scope="", project_names=()).applied is True


# ── the wire and the stage ───────────────────────────────────────────────────────────────────────────────


def test_the_envelope_shape_is_unchanged_when_the_tier_was_not_consulted() -> None:
    plain = build_evidence_request({"source": "RDR-084"}, scope="", name_lookup=str)
    assert plain.active_project is None and "active_project" not in (plain.to_wire() or {})
    assert OwnerResolution("inferred", ART_OWNER, (("active_project", ART_OWNER),)).to_wire()["status"] == "inferred"


@pytest.mark.asyncio
async def test_the_stage_asks_the_catalog_only_when_there_is_something_to_qualify(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    explicit = _request({"owner": NEXUS_OWNER})
    tumbler_only = build_evidence_request({"source": NEXUS_DOC}, scope="", name_lookup=str)
    no_source = build_evidence_request({"owner": "1.1"}, scope="", name_lookup=str)
    no_binding = build_evidence_request(None, scope="rdr", name_lookup=str)
    with patch.object(core, "_get_catalog", side_effect=AssertionError("the catalog must not be asked")), \
         patch("nexus.plans.active_project.detect_active_project", side_effect=AssertionError("not consulted")):
        for req, scope in ((explicit, ""), (tumbler_only, ""), (no_source, ""), (no_binding, "rdr"),
                           (_request(), NEXUS_OWNER)):
            assert await core._nx_answer_active_project(req, (), scope=scope, deadline=None) is req


@pytest.mark.asyncio
async def test_the_stage_applies_the_tier_in_a_worker_thread_and_never_raises(tmp_path: Path) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    with patch.object(core, "_get_catalog", return_value=cat), \
         patch("nexus.plans.active_project.detect_active_project",
               side_effect=lambda c=None, **kw: detect_active_project(
                   c, get_catalog=kw.get("get_catalog"), environ=environ, getcwd=lambda: cwd)):
        got = await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
    assert got.owner.status == "inferred" and got.owner.tumbler == ART_OWNER and got.active_project["applied"] is True

    with patch.object(core, "_get_catalog", return_value=cat), \
         patch("nexus.plans.active_project.detect_active_project", side_effect=RuntimeError("boom")):
        failed = await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
    assert failed.owner.status == "none" and failed.active_project["applied"] is False
    assert "RuntimeError" in failed.active_project["reason"]

    with patch.object(core, "_get_catalog", side_effect=OSError("no catalog")):
        no_cat = await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
    assert no_cat.owner.status == "none" and no_cat.active_project["applied"] is False
