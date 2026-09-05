# SPDX-License-Identifier: AGPL-3.0-or-later
import ast
import contextlib
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.hooks import _stanza_for
from nexus.db.http_vector_client import HttpVectorClient
from tests._catalog_fixture_ops import ActiveCatalog, only_document

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_cloud_credentials_from_host_env(monkeypatch, tmp_path):
    """nexus-m7evs: the credential-persistence health check reads
    ``os.environ`` and ``~/.config/nexus/config.yml`` directly (not
    through ``get_credential``) so it can detect env-vs-file divergence.
    Tests that mock ``get_credential`` for other scenarios get the new
    check piggy-backing on their fixtures. Strip the cloud credentials
    from the test process env and point the config dir at a clean
    tmp_path so the new check is silent unless a test explicitly opts in.
    """
    for env_var in ("CHROMA_API_KEY", "VOYAGE_API_KEY", "CHROMA_TENANT", "CHROMA_DATABASE"):
        monkeypatch.delenv(env_var, raising=False)
    # Redirect via env, NOT monkeypatch.setattr("nexus.config.
    # nexus_config_dir", ...): nexus_config_dir() reads NEXUS_CONFIG_DIR at
    # call time (config.py), so setenv reaches every consumer and leaves no
    # lambda behind. The setattr form leaked out of this file — the doctor
    # runs below FIRST-import nexus.gc_purge_marker (deferred import in
    # health._check_gc_audit_non_empty_after_purge) inside the patched
    # window, and a module capturing nexus_config_dir by value keeps the
    # lambda after teardown, pinning that consumer to this fixture's dead
    # tmp dir for the rest of the worker process (2026-08-20: broke three
    # TestGcPurgeMarker tests in tests/test_health_service_checks.py under
    # -n auto; identical to the v7.11.0 / PR #1467 incident recorded in
    # tests/test_false_clean_diagnostics_service_mode.py).
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def mock_reg():
    reg = MagicMock()
    reg.all.return_value = []
    return reg


def _invoke(runner, mock_reg, *, cred="sk-key", which="/usr/bin/tool",
            cloud_client=None, extra_patches=None, extra_args=None):
    # RDR-137 Phase 3.1 (nexus-tts0d.6): health.py now reads repos via
    # nexus.repos.list_repos_dual instead of RepoRegistry directly.
    # The legacy RepoRegistry patch stays for whatever doctor sub-checks
    # still reach it (e.g. the integrity path); the new patch wires the
    # catalog-backed reader to the same mock so existing test
    # expectations (``/some/repo`` appears in hook output) keep
    # holding. Once the doctor.py cutover (nexus-tts0d.12) lands the
    # legacy patch becomes ineffective and can drop.
    patches = [
        patch("nexus.config.is_local_mode", return_value=False),
        patch("nexus.registry.RepoRegistry", return_value=mock_reg),
        # Patch at definition site (nexus.repos) because health.py
        # imports lazily inside _check_hooks — no module-level
        # symbol exists to patch on nexus.health itself.
        #
        # nexus-cw262: health.py's git-hooks check now calls
        # list_repos_dual_with_catalog_roots directly (one round trip
        # serving both the walk list and the catalog-only attribution set,
        # instead of a second independent list_owners_by_type call) —
        # patch that function, not the list_repos_dual wrapper it no
        # longer goes through. catalog_repo_roots=set() mirrors this
        # fixture's mock_reg-only (no catalog) setup.
        patch(
            "nexus.repos.list_repos_dual_with_catalog_roots",
            side_effect=lambda **_: (list(mock_reg.all()), set(), "unknown"),
        ),
    ]
    if callable(cred):
        patches.append(patch("nexus.config.get_credential",
                             side_effect=cred))
    else:
        patches.append(patch("nexus.config.get_credential",
                             return_value=cred))
    if callable(which):
        # nexus-l2ku5 round 2: _resolve_mcp_binary calls the real
        # shutil.which signature (``shutil.which(name, path=directory)``)
        # to walk PATH per-entry; test doubles here predate that and only
        # accept a single positional `name`. Shield them so every existing
        # `which=` callable keeps working unchanged.
        def _which_side_effect(name, *_args, **_kwargs):
            return which(name)
        patches.append(patch("nexus.health.shutil.which",
                             side_effect=_which_side_effect))
    else:
        patches.append(patch("nexus.health.shutil.which",
                             return_value=which))
    # nexus-l2ku5: `which` above is faked to a placeholder path for every
    # binary name (including nx-mcp / nx-mcp-catalog) that doesn't
    # correspond to a real MCP entry point on disk — spawning it for a
    # real JSON-RPC handshake would fail for reasons unrelated to whatever
    # this test actually exercises. Stub the probe green by default;
    # tests/test_health_mcp_entrypoints.py owns the real handshake
    # behavior (crash / timeout / wrong-name / absent-binary), unit-tested
    # directly against `_check_mcp_entry_points` / `_probe_mcp_server`.
    patches.append(patch("nexus.health._probe_mcp_server",
                         return_value=(True, "stubbed for unrelated doctor test")))
    # RDR-155 P4a.2 (nexus-1k8s1): the cloud reachability probe targets the
    # nexus-service vector surface (nexus.db.http_vector_client._get), not a
    # chromadb.CloudClient construction. ``cloud_client`` keeps its kwarg name
    # for the existing call sites but patches the service probe.
    if cloud_client is not None:
        patches.append(patch("nexus.db.http_vector_client._get",
                             **cloud_client))
    else:
        # The probe runs UNCONDITIONALLY post-cutover (critique finding 2) —
        # always stub it so no doctor invocation makes a real HTTP attempt.
        patches.append(patch("nexus.db.http_vector_client._get",
                             return_value=[]))
    patches.extend(extra_patches or [])
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return runner.invoke(main, ["doctor", *(extra_args or [])])


# ── Healthy / basic output ──────────────────────────────────────────────────

def test_doctor_all_healthy(runner, mock_reg):
    result = _invoke(runner, mock_reg)
    assert result.exit_code == 0
    assert "\u2713" in result.output


@pytest.mark.parametrize("expected", [
    "git hooks", "index log", "\u2713 index log", "Python", "3.12",
])
def test_doctor_healthy_output_contains(runner, mock_reg, expected):
    result = _invoke(runner, mock_reg)
    assert result.exit_code == 0
    assert expected in result.output


@pytest.mark.parametrize("absent", ["nx serve start", "Nexus server"])
def test_doctor_does_not_mention_serve(runner, mock_reg, absent):
    result = _invoke(runner, mock_reg)
    assert absent not in result.output


# ── Supplementary checks (default-sweep promotion of the cheap/read-only ────
# ── opt-in --check-* subset) ─────────────────────────────────────────────────


class TestSupplementaryChecks:
    """The default `nx doctor` sweep additionally runs the promoted subset
    of --check-* diagnostics (resources / plan-library / taxonomy /
    aspect-queue / t1) and names the rest as opt-in-only. Non-gating: a
    supplementary check's failure (e.g. an unreachable engine in this
    unit-test environment, which has none) is printed but never flips the
    sweep's own exit code -- see doctor.py's classification-table comment
    for why a uniform pass/fail mapping across five differently-shaped
    checks would be unsound.
    """

    def test_section_header_present(self, runner, mock_reg):
        result = _invoke(runner, mock_reg)
        assert result.exit_code == 0
        assert "Supplementary checks" in result.output

    @pytest.mark.parametrize("marker", [
        "--- resources ---",
        "--- plan-library ---",
        "--- taxonomy ---",
        "--- aspect-queue ---",
        "--- t1 ---",
        "--- engine-activity ---",
    ])
    def test_each_promoted_check_runs(self, runner, mock_reg, marker):
        result = _invoke(runner, mock_reg)
        assert result.exit_code == 0
        assert marker in result.output

    def test_remaining_opt_in_checks_named(self, runner, mock_reg):
        result = _invoke(runner, mock_reg)
        assert "Remaining opt-in-only checks" in result.output
        for flag in (
            "--check-schema", "--check-search", "--check-quotas",
            "--check-mcp-logs", "--check-tier-discipline",
            "--check-storage-boundary", "--check-post-store-hooks",
            "--check-mineru", "--check-wal-retention",
        ):
            assert flag in result.output

    def test_promoted_checks_not_opt_in_named(self, runner, mock_reg):
        """The five promoted flags must NOT appear in the opt-in-only
        summary line -- they already ran above it."""
        result = _invoke(runner, mock_reg)
        tail = result.output.rsplit("Remaining opt-in-only checks", 1)[-1]
        for absent in (
            "--check-resources", "--check-plan-library",
            "--check-taxonomy", "--check-aspect-queue", "--check-t1",
            "--check-engine-activity",
        ):
            assert absent not in tail

    def test_unreachable_engine_does_not_flip_exit_code(self, runner, mock_reg):
        """No engine is configured in this unit-test environment, so every
        HTTP-backed supplementary check (plan-library / taxonomy /
        aspect-queue) reports 'unreachable'. That must stay informational
        -- the overall sweep still exits 0 for an otherwise-healthy
        install (non-gating design)."""
        result = _invoke(runner, mock_reg)
        assert result.exit_code == 0
        assert "unreachable" in result.output or "UNKNOWN" in result.output

    def test_json_mode_omits_supplementary_section(self, runner, mock_reg):
        """--json is machine-parseable stdout only -- no human prose mixed
        in (nexus-0vycz). The supplementary section must not run there."""
        result = _invoke(runner, mock_reg, extra_args=["--json"])
        assert "Supplementary checks" not in result.output

    def test_one_check_raising_does_not_abort_the_rest(self, runner, mock_reg):
        """A check that raises something other than click.exceptions.Exit
        must not prevent the remaining supplementary checks (or the
        opt-in-only summary line) from running."""
        result = _invoke(
            runner, mock_reg,
            extra_patches=[
                patch(
                    "nexus.commands.doctor._run_check_resources",
                    side_effect=RuntimeError("boom"),
                ),
            ],
        )
        assert result.exit_code == 0
        assert "resources check raised unexpectedly" in result.output
        # The rest of the sweep still ran.
        assert "--- t1 ---" in result.output
        assert "Remaining opt-in-only checks" in result.output


# ── Missing credentials ─────────────────────────────────────────────────────

def test_doctor_missing_credentials_informational(runner, mock_reg):
    """nexus-nmw3i/c7aj3 → RDR-155 P4b: the CHROMA_* credential rows died
    with the migration machinery; the Voyage row survives and stays
    informational — absent creds are never a failing/fatal doctor result
    (the exit-1 false-positive on migrated installs)."""
    result = _invoke(runner, mock_reg, cred=None)
    assert "CHROMA_API_KEY" not in result.output  # row deleted at P4b
    assert "VOYAGE_API_KEY" in result.output
    # Absent creds alone must not produce the fatal ✗ + exit 1 shape or
    # push credential setup on a serving-healthy install.
    assert "nx config init" not in result.output
    assert "nx config set chroma_api_key" not in result.output


def test_doctor_partial_credentials_informational(runner, mock_reg):
    """The Voyage row reads 'set' when present; no CHROMA rows remain
    (RDR-155 P4b) — no fatal line either way (nexus-nmw3i/c7aj3)."""
    def cred_side_effect(key):
        return "sk-key" if key == "voyage_api_key" else None

    result = _invoke(runner, mock_reg, cred=cred_side_effect)
    assert "CHROMA_DATABASE" not in result.output  # row deleted at P4b
    assert "VOYAGE_API_KEY" in result.output
    assert "nx config set chroma_database" not in result.output


# ── Missing tools ───────────────────────────────────────────────────────────

def _which_missing(name):
    """which side-effect that only hides rg."""
    return None if name == "rg" else f"/usr/bin/{name}"


def test_doctor_missing_rg(runner, mock_reg):
    """nexus-9xfx5 (fresh-install MVV finding #3): rg is an optional system
    accelerator pip can never provide — its absence renders like an
    uninstalled git hook (✓ + detail + install hints), NOT a failed doctor.
    Exit 0: a virgin box without ripgrep is healthy, just degraded."""
    result = _invoke(runner, mock_reg, which=_which_missing)
    assert result.exit_code == 0
    assert "not installed" in result.output
    assert "hybrid search disabled" in result.output
    assert "brew install ripgrep" in result.output


def test_doctor_missing_rg_shows_platform_hints(runner, mock_reg):
    result = _invoke(runner, mock_reg, which=lambda _: None)
    assert "brew install ripgrep" in result.output
    assert "apt install ripgrep" in result.output
    assert "BurntSushi/ripgrep" in result.output


def test_doctor_missing_rg_includes_winget_hint(runner, mock_reg):
    """nexus-njmg (GH #622): the Fix-line block for ripgrep must
    include a Windows winget command. Operators on Windows had no
    actionable install line and had to leave the terminal to figure
    out the path manually. ``--scope user`` is mandatory to avoid
    UAC-prompt failures during unattended install.
    """
    result = _invoke(runner, mock_reg, which=lambda _: None)
    assert "winget install --id BurntSushi.ripgrep.MSVC" in result.output
    assert "--scope user" in result.output


def test_doctor_missing_git_includes_winget_hint(runner, mock_reg):
    """nexus-njmg: git Fix-line must include winget."""
    def which_side(name):
        return None if name == "git" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert "winget install --id Git.Git --scope user" in result.output


def test_doctor_missing_npx_includes_winget_hint(runner, mock_reg):
    """nexus-njmg: Node.js (npx) Fix-line must include winget so
    Windows plugin users can install the MCP-server runtime.
    """
    def which_side(name):
        return None if name == "npx" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert "winget install --id OpenJS.NodeJS.LTS --scope user" in result.output


def test_doctor_missing_bd_includes_release_zip_hint(runner, mock_reg):
    """nexus-njmg: bd has no winget package; the Fix-line must point
    at the GitHub releases page so Windows users can find the right
    binary.
    """
    def which_side(name):
        return None if name == "bd" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert "github.com/BeadsProject/beads/releases" in result.output


@pytest.mark.parametrize("tool,exit_code", [("bd", 0), ("uv", 0)])
def test_doctor_missing_optional_tool(runner, mock_reg, tool, exit_code):
    def which_side(name):
        return None if name == tool else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert result.exit_code == exit_code


def test_doctor_missing_bd_output(runner, mock_reg):
    def which_side(name):
        return None if name == "bd" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert "bd (beads" in result.output
    assert "not found" in result.output
    assert "BeadsProject/beads" in result.output


def test_doctor_missing_npx_is_non_fatal_with_plugin_hint(runner, mock_reg):
    """Missing npx is plugin-only — non-fatal for the CLI but reported with
    a clear hint that the plugin's MCP servers will fail without it."""
    def which_side(name):
        return None if name == "npx" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert result.exit_code == 0, "missing npx must not fail nx doctor (plugin-only)"
    assert "npx (Node.js, plugin-only)" in result.output
    assert "not found" in result.output
    assert "sequential-thinking" in result.output or "context7" in result.output
    assert "nodejs.org" in result.output


# ── Python version ──────────────────────────────────────────────────────────

def test_doctor_python_version_too_old_fails(runner, mock_reg):
    result = _invoke(runner, mock_reg, extra_patches=[
        patch("nexus.health._python_ok", return_value=(False, "3.11.0")),
    ])
    # nexus-be6x8 (7.21.0): the Python floor is a FATAL check -> exit 2.
    assert result.exit_code == 2
    assert "\u2717" in result.output
    assert "3.12" in result.output
    assert "python.org" in result.output


# ── Hooks ───────────────────────────────────────────────────────────────────

def test_doctor_hooks_no_repos_registered(runner, mock_reg):
    result = _invoke(runner, mock_reg)
    assert result.exit_code == 0
    assert "no repos registered" in result.output
    assert "nx index repo" in result.output
    assert "\u2713 git hooks" in result.output


def test_doctor_hooks_installed(runner):
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    with tempfile.TemporaryDirectory() as td:
        hooks_dir = Path(td)
        # A real installed stanza, not a begin-sentinel with no end: the
        # old drift comparison returned "no body" for that malformed shape
        # and reported it green; the shared hook_stanza_state (nexus-trwxr)
        # calls it stale, which is the honest answer.
        for name in ("post-commit", "post-merge", "post-rewrite"):
            (hooks_dir / name).write_text(f"#!/bin/sh\n{_stanza_for(name)}\n")
        result = _invoke(runner, reg, extra_patches=[
            patch("nexus._git_hooks_meta.effective_hooks_dir",
                  return_value=hooks_dir),
        ])
    assert result.exit_code == 0, result.output
    assert "\u2713 git hooks" in result.output
    assert "/some/repo" in result.output
    assert "post-commit" in result.output


def test_doctor_reports_a_malformed_stanza_as_drift(runner):
    """Begin sentinel, no end sentinel: previously green by accident."""
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    with tempfile.TemporaryDirectory() as td:
        hooks_dir = Path(td)
        (hooks_dir / "post-commit").write_text(
            f"#!/bin/sh\n{SENTINEL_BEGIN}\nnx index repo ...\n")
        result = _invoke(runner, reg, extra_patches=[
            patch("nexus._git_hooks_meta.effective_hooks_dir",
                  return_value=hooks_dir),
        ])
    assert "stanza differs" in result.output


def test_doctor_hooks_not_installed(runner):
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    with tempfile.TemporaryDirectory() as td:
        result = _invoke(runner, reg, extra_patches=[
            patch("nexus._git_hooks_meta.effective_hooks_dir",
                  return_value=Path(td)),
        ])
    assert result.exit_code == 0
    assert "\u2713 git hooks" in result.output
    assert "not installed" in result.output
    assert "nx hooks install /some/repo" in result.output
    assert "\u2717 git hooks" not in result.output


def test_doctor_hooks_exception_does_not_propagate(runner):
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    result = _invoke(runner, reg, extra_patches=[
        patch("nexus._git_hooks_meta.effective_hooks_dir",
              side_effect=RuntimeError("git error")),
    ])
    assert result.exit_code == 0
    assert "git hooks" in result.output


def test_doctor_git_hooks_scope_excludes_out_of_scope_repo(runner, tmp_path):
    """nexus-jds59: ``--git-hooks-scope`` wires through to the CLI. A
    registered repo outside the given root (the ambient dev-checkout
    class of repo) is excluded from the walk instead of being reported,
    while one inside the root is still shown."""
    reg = MagicMock()
    in_scope_root = tmp_path / "scope"
    in_scope_root.mkdir()
    repo_in_scope = str(in_scope_root / "sandbox-fixture")
    repo_outside = str(tmp_path / "outside" / "dev-checkout")
    reg.all.return_value = [repo_in_scope, repo_outside]
    with tempfile.TemporaryDirectory() as td:
        result = _invoke(runner, reg, extra_patches=[
            patch("nexus._git_hooks_meta.effective_hooks_dir",
                  return_value=Path(td)),
        ], extra_args=["--git-hooks-scope", str(in_scope_root)])
    assert result.exit_code == 0, result.output
    assert repo_in_scope in result.output
    assert repo_outside not in result.output


# ── Index log ───────────────────────────────────────────────────────────────

def test_doctor_index_log_not_created_yet(runner, mock_reg):
    """nexus-ay18d fix-round finding: this assertion previously passed via
    an UNRELATED check's "not created yet" wording — the now-retired
    ``_check_t2_integrity``'s fresh-box detail, present ANYWHERE in the
    full doctor output because the assertion is a blanket substring check,
    not scoped to the index-log section. The index-log check's own
    no-activity wording never actually said "not created yet". Pinned to
    the real message instead."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        (fake_home / ".config" / "nexus").mkdir(parents=True, exist_ok=True)
        result = _invoke(runner, mock_reg, extra_patches=[
            patch.object(Path, "home", return_value=fake_home),
        ])
    assert "index log" in result.output
    assert "no index activity recorded yet" in result.output


# ── Single-database check ───────────────────────────────────────────────────

def test_doctor_probes_vector_service(runner, mock_reg):
    # RDR-155 P4a.2: the probe hits the service's /v1/vectors/collections.
    result = _invoke(runner, mock_reg,
                     cloud_client={"return_value": []})
    assert "Vector service" in result.output
    assert "not reachable" not in result.output


def test_doctor_single_db_unreachable_fails(runner, mock_reg):
    result = _invoke(runner, mock_reg, cloud_client={
        "side_effect": RuntimeError("connection refused"),
    })
    # nexus-be6x8 (7.21.0): an unreachable store is a FATAL check -> exit 2.
    assert result.exit_code == 2
    assert "not reachable" in result.output
    assert "NX_SERVICE_URL" in result.output


def test_doctor_single_db_no_secret_leak(runner, mock_reg):
    result = _invoke(runner, mock_reg, cloud_client={
        "side_effect": RuntimeError("HTTP 401: invalid api_key SUPERSECRET"),
    })
    assert "SUPERSECRET" not in result.output
    assert "not reachable" in result.output


# ── _check helper ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ok,expected", [
    (True, "\u2713"), (False, "\u2717"),
])
def test_check_helper_format(ok, expected):
    from nexus.commands.doctor import _check
    assert expected in _check("Test", ok)


def test_check_helper_detail():
    from nexus.commands.doctor import _check
    assert "some detail" in _check("Test", True, "some detail")


# ── Local mode ──────────────────────────────────────────────────────────────

def test_doctor_local_mode_shows_local_checks(runner, mock_reg, tmp_path):
    # RDR-137 followup IMP-22 (nexus-43qgm.22): the legacy
    # nexus.registry.RepoRegistry patch is dead code post-RDR-137
    # (health.py routes through nexus.repos.list_repos_dual). Patch
    # the live path instead so the test exercises actual production
    # code rather than passing for unrelated reasons.
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.health.shutil.which", return_value="/usr/bin/rg"),
        # nexus-l2ku5: stub the real handshake — /usr/bin/rg is not an MCP
        # entry point; the real behavior is unit-tested directly in
        # tests/test_health_mcp_entrypoints.py.
        patch("nexus.health._probe_mcp_server", return_value=(True, "stubbed")),
        # nexus-cw262: health.py's git-hooks check now calls
        # list_repos_dual_with_catalog_roots directly; the old
        # list_repos_dual wrapper is no longer on that path.
        patch(
            "nexus.repos.list_repos_dual_with_catalog_roots",
            side_effect=lambda **_: (list(mock_reg.all()), set(), "unknown"),
        ),
        # Unconditional service probe (critique finding 2) — stub it green.
        patch("nexus.db.http_vector_client._get", return_value=[]),
    ):
        result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "local" in result.output.lower()
    assert "Embedding model" in result.output
    assert "CHROMA_API_KEY" not in result.output
    assert "VOYAGE_API_KEY" not in result.output


def test_doctor_local_mode_shows_collection_count(runner, mock_reg, tmp_path):
    """RDR-155 P4a.2 (nexus-1k8s1): the collection census routes through the
    pgvector service handle (``make_t3()``); the on-disk chroma directory is
    reported as the legacy store awaiting the P5 ETL.
    """
    chroma_path = tmp_path / "chroma"
    chroma_path.mkdir()
    (chroma_path / "blob.bin").write_bytes(b"x" * 1024)

    class _StubServiceClient:
        def list_collections(self):
            return [{"name": "knowledge__test"}]

    # RDR-137 followup IMP-22 (nexus-43qgm.22): patch the live
    # nexus.repos.list_repos_dual path, not the dead nexus.registry
    # one (see sibling test for rationale).
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config.local_embed_model_choice", return_value="all-MiniLM-L6-v2"),
        patch("nexus.health.shutil.which", return_value="/usr/bin/rg"),
        # nexus-l2ku5: stub the real handshake — /usr/bin/rg is not an MCP
        # entry point; the real behavior is unit-tested directly in
        # tests/test_health_mcp_entrypoints.py.
        patch("nexus.health._probe_mcp_server", return_value=(True, "stubbed")),
        # nexus-cw262: health.py's git-hooks check now calls
        # list_repos_dual_with_catalog_roots directly; the old
        # list_repos_dual wrapper is no longer on that path.
        patch(
            "nexus.repos.list_repos_dual_with_catalog_roots",
            side_effect=lambda **_: (list(mock_reg.all()), set(), "unknown"),
        ),
        patch("nexus.db.make_t3", return_value=_StubServiceClient()),
        # Unconditional service probe (critique finding 2) — stub it green.
        patch("nexus.db.http_vector_client._get", return_value=[]),
    ):
        result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "1 collections" in result.output
    # RDR-155 P4b: the legacy-Chroma "on disk" size report died with the
    # migration machinery — the census is the pgvector service count only.
    #
    # Pinned on the census LINE rather than on the whole output. The bare
    # substring stopped meaning what it says once an unrelated check began
    # reporting templates "on disk" (nexus-f1mbo's plan-library parity),
    # and a pin that fails on a phrase appearing anywhere is a pin that
    # will keep failing for reasons it does not care about.
    census_line = next(
        line for line in result.output.splitlines() if "1 collections" in line
    )
    assert "on disk" not in census_line
    assert "MB" not in census_line


# ── doctor --fix-paths ─────────────────────────────────────────────────────


class TestFixPaths:
    """doctor --fix-paths migration tests."""

    @pytest.fixture(autouse=True)
    def _git_identity(self, monkeypatch):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")

    @pytest.fixture(autouse=True)
    def _point_catalog_path(self, tmp_path, monkeypatch):
        """Aim ``catalog_path()`` at the dir these tests seed (nexus-aqbrk).

        The tests patch ``nexus.config.catalog_path`` inside their ``with``
        block, but :class:`ActiveCatalog` resolves through the same factories
        the verb uses and those read the env on the SQLite arm, so the two
        have to agree from fixture time.
        """
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))

    def _make_catalog_with_entries(self, tmp_path, entries):
        """Seed the ACTIVE catalog with the given entries (nexus-aqbrk).

        CONVERTED, not pinned, because ``doctor --fix-paths`` genuinely runs
        on both substrates: it reads ``reader.docs_with_absolute_paths()``,
        which doctor.py:1856 documents as "uniform across SQLite and service
        mode" (one of the endpoints nexus-xnz0o ported). The old form built a
        LOCAL ``Catalog.init`` and the verb then read the SERVICE catalog, so
        it found nothing.

        That mismatch did not only fail two tests — it made two others pass
        VACUOUSLY: ``test_fix_paths_skips_curator`` asserts
        ``update_source_path.assert_not_called()`` and
        ``test_fix_paths_idempotent`` asserts ``"No absolute" in output``,
        both trivially true against an empty catalog. Seeding the active
        catalog restores their subject.

        entries: list of (owner_type, repo_hash, repo_root, file_path, collection).
        """
        # nexus-i711w: the local Catalog.init that used to run here died with
        # the local catalog; the factories are service-only, so seeding goes
        # straight through the active (service) catalog.
        cat = ActiveCatalog()
        for owner_type, repo_hash, repo_root, file_path, collection in entries:
            owner = cat.register_owner(
                f"test-{repo_hash or 'curator'}",
                owner_type,
                repo_hash=repo_hash,
                repo_root=repo_root,
            )
            cat.register(
                owner,
                "test-doc",
                content_type="code",
                file_path=file_path,
                physical_collection=collection,
            )
        return cat, tmp_path / "catalog"

    def test_fix_paths_dry_run(self, tmp_path, runner):
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("repo", "abc12345", str(tmp_path / "repo"),
             str(tmp_path / "repo" / "src" / "foo.py"), "code__test"),
        ])
        # make_t3() returns the service-backed HttpVectorClient
        # UNCONDITIONALLY in production since RDR-155 P4a.2 -- cloud
        # creds / is_local_mode() no longer affect the handle type, and
        # --fix-paths has no isinstance/is_service_backed branch that
        # would care either way. spec= it so a missing-method bug can't
        # hide behind an unspec'd stub.
        mock_t3 = MagicMock(spec=HttpVectorClient)
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths", "--dry-run"])
        assert result.exit_code == 0
        assert "[dry-run]" in result.output
        assert "src/foo.py" in result.output
        # Verify nothing was actually changed
        assert only_document().file_path.startswith("/"), "dry-run must not rewrite"

    def test_fix_paths_writes_relative(self, tmp_path, runner):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("repo", "abc12345", str(repo_dir),
             str(repo_dir / "src" / "foo.py"), "code__test"),
        ])
        # nexus-bm8dd: fix-paths no longer touches T3 at all. It used to call
        # update_source_path first and report the count as "(n chunks)"; chunk
        # metadata has carried no source_path since RDR-102 D2, so that call
        # rewrote nothing and n was always 0. This test asserted it was CALLED —
        # a pin on the call, never on its effect, which is how the dead leg
        # survived. The repair is entirely the catalog row.
        mock_t3 = MagicMock(spec=HttpVectorClient)
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0
        assert "Fixed 1" in result.output
        assert only_document().file_path == "src/foo.py"
        # The path repair landed WITHOUT any T3 call — and the output must not
        # advertise a chunk-level component the command does not have.
        mock_t3.update_source_path.assert_not_called()
        assert "chunks updated" not in result.output

    def test_fix_paths_skips_curator(self, tmp_path, runner):
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("curator", "", "", "/abs/path/paper.pdf", "docs__papers"),
        ])
        mock_t3 = MagicMock(spec=HttpVectorClient)
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0
        # NON-VACUITY (nexus-aqbrk): assert_not_called() holds trivially when
        # nothing was seeded, which is exactly how this passed before the
        # conversion — against an empty service catalog. Prove the curator row
        # with an absolute path IS present, so "not called" means "skipped by
        # owner_type", not "found nothing".
        seeded = only_document()
        assert seeded.file_path == "/abs/path/paper.pdf", (
            f"seed did not land; the skip assertion below would be vacuous "
            f"(got {seeded.file_path!r})"
        )
        # nexus-bm8dd: T3 is not touched on ANY fix-paths path now, so this
        # assertion no longer distinguishes "skipped the curator" by itself —
        # the seed check above is what carries it.
        mock_t3.update_source_path.assert_not_called()

    def test_fix_paths_idempotent(self, tmp_path, runner):
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("repo", "abc12345", str(tmp_path / "repo"),
             "src/foo.py", "code__test"),  # already relative
        ])
        mock_t3 = MagicMock(spec=HttpVectorClient)
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0
        assert "No absolute" in result.output
        # NON-VACUITY (nexus-aqbrk): "No absolute" is also what an EMPTY
        # catalog reports, so pin that the already-relative row exists and was
        # left alone — otherwise this asserts nothing about idempotence.
        assert only_document().file_path == "src/foo.py"
        mock_t3.update_source_path.assert_not_called()


# ── --check-quotas (nexus-c590) ─────────────────────────────────────────────


class TestCheckQuotas:
    """``nx doctor --check-quotas`` surfaces the free-tier cloud limits,
    Voyage AI per-model token caps, and any retry load observed this
    process. Exits 0 when the cloud tenant is reachable (or when we're
    in local mode); exits 1 when the cloud probe fails so the report is
    actionable via exit code (nexus-c590 acceptance)."""

    @pytest.fixture(autouse=True)
    def _reset_retry_counters_between_tests(self):
        """The retry accumulator is process-local state; isolate each
        test so prior runs can't leak counts into the next case's
        assertion (same contract as tests/test_voyage_retry.py)."""
        from nexus.retry import reset_retry_stats
        reset_retry_stats()
        yield
        reset_retry_stats()

    def test_reachable_cloud_exits_zero_and_reports_all_sections(
        self, runner: CliRunner,
    ) -> None:
        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock(spec=HttpVectorClient)),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        # Vector-store section (nexus-d01js relabel: T3 is pgvector since
        # 6.0.0; the limits table stays as the Chroma-era reference) + a
        # representative limit
        assert "T3 vector store" in result.output
        assert "T3 backend reachable" in result.output
        assert "max_records_per_write" in result.output
        assert "300" in result.output  # MAX_RECORDS_PER_WRITE / MAX_QUERY_RESULTS
        # Voyage section
        assert "Voyage AI" in result.output
        assert "voyage-code-3" in result.output
        assert "32,000" in result.output  # 32k token cap rendered with comma
        # Retry-accumulator quiet case
        assert "no transient backoffs observed" in result.output

    def test_unreachable_cloud_exits_one(self, runner: CliRunner) -> None:
        """A quota report without a client connection is not actionable.
        The command must exit 1 so CI / operators can gate on it."""

        def _raise(*_a, **_kw):
            raise RuntimeError("simulated cloud outage")

        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", side_effect=_raise),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 1, result.output
        assert "unreachable" in result.output
        assert "RuntimeError" in result.output

    def test_local_mode_exits_zero_even_without_cloud(
        self, runner: CliRunner,
    ) -> None:
        """Local mode (``NX_LOCAL=1``) doesn't need cloud — the limits
        are reference-only in that context. Exit 0."""
        with (
            patch("nexus.config.is_local_mode", return_value=True),
            patch("nexus.config.get_credential", return_value=""),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        assert "local mode" in result.output

    def test_voyage_key_absent_shows_warn_marker(
        self, runner: CliRunner,
    ) -> None:
        """When ``VOYAGE_API_KEY`` is not set the voyage section must
        use the warn marker (``✗``) so the report is scannable at a
        glance, even though cloud itself may be reachable."""
        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock(spec=HttpVectorClient)),
            patch("nexus.config.get_credential", return_value=""),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        assert "VOYAGE_API_KEY: absent" in result.output

    def test_retry_counters_surface_when_nonzero(
        self, runner: CliRunner,
    ) -> None:
        """A session that has hit transient errors shows the cumulative
        backoff time + count under "Observed transient-error retries"."""
        from nexus.retry import _add_vector_retry, _add_voyage_retry

        _add_voyage_retry(1.5)
        _add_voyage_retry(3.0)
        _add_vector_retry(2.0)

        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock(spec=HttpVectorClient)),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        assert "Observed transient-error retries" in result.output
        assert "voyage:" in result.output
        assert "2 retries" in result.output       # voyage count
        assert "chroma:" in result.output
        assert "1 retries" in result.output       # chroma count
        # Total aggregates both sides
        assert "3 retries" in result.output       # total_count
        assert "6.5s" in result.output            # 1.5 + 3.0 + 2.0 total_seconds

    def test_json_output_has_structured_schema(
        self, runner: CliRunner,
    ) -> None:
        """``--json`` returns a parseable dict with the three expected
        top-level sections so downstream tools (dashboards, CI gates)
        can key off the schema."""
        import json as _json

        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock(spec=HttpVectorClient)),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(
                main, ["doctor", "--check-quotas", "--json"]
            )
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)
        # RDR-155 P4b: the section key is "vector_store", NOT "chromadb".
        # It carried the dependency's name for machine-consumer stability
        # ("until P4b renames it", doctor.py's own comment) and P4b is the
        # wave that removed the dependency. Renamed in 7.0.0 because a MAJOR
        # is the only defensible moment to break a JSON contract that
        # cli-reference.md advertises for "dashboards / CI gates".
        # The exact-set assertion below is what pins the rename: a stray
        # "chromadb" key coming back fails it.
        assert set(data.keys()) == {
            "vector_store", "voyage", "cross_encoder", "retry",
        }
        assert "chromadb" not in data, (
            "the retired dependency's name is back as a JSON section key"
        )
        assert data["vector_store"]["reachable"] is True
        assert data["vector_store"]["limits"]["max_records_per_write"] == 300
        assert "voyage-code-3" in data["voyage"]["models"]
        assert data["voyage"]["api_key_set"] is True
        assert data["retry"]["total_count"] == 0

# ── --check-t1 (RDR-105 P5 / nexus-ssdg) ─────────────────────────────────────


class TestCheckT1:
    """Diagnostic: T1 session lease presence + freshness.

    Ported (nexus-8zfwv, 2026-08-07) off the RDR-149 P4 ``t1_addr.*``
    ``ServiceRegistry`` lease -- ``T1LeasePublisher``, the only thing that
    ever published that format, is retired (deleted at ff744321). The
    fixture below publishes via the REAL ``nexus.db.t1.publish_t1_session_lease``
    (never a hand-built filename), so these tests exercise the SAME file
    ``_run_check_t1`` reads.
    """

    @staticmethod
    def _publish_lease(config_dir, session_id, *, ttl_seconds):
        from nexus.db.t1 import publish_t1_session_lease

        publish_t1_session_lease(session_id, "tok", Path(config_dir), ttl_seconds=ttl_seconds)

    def test_no_session_id_is_informational(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        result = runner.invoke(main, ["doctor", "--check-t1"])
        assert result.exit_code == 0, result.output
        assert "no session-id resolves" in result.output.lower()

    def test_no_lease_file_under_resolved_session_is_informational(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        """A resolved session with NO lease file is not a failure -- a bare
        CLI invocation legitimately has no lease of its own (the MCP
        lifespan mints one; the CLI uses its own dedicated scope)."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        result = runner.invoke(main, ["doctor", "--check-t1"])
        assert result.exit_code == 0, result.output
        assert "no lease at" in result.output

    def test_lease_present_but_expired_fails(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        self._publish_lease(tmp_path, "sess-A", ttl_seconds=-1.0)
        result = runner.invoke(main, ["doctor", "--check-t1"])
        assert result.exit_code == 1, result.output
        assert "expired or unreadable" in result.output

    def test_healthy(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        self._publish_lease(tmp_path, "sess-A", ttl_seconds=3600.0)
        result = runner.invoke(main, ["doctor", "--check-t1"])
        assert result.exit_code == 0, result.output
        assert "is fresh" in result.output


# ── --check-wal-retention (nexus-bb5c8) ──────────────────────────────────────


class TestCheckWalRetention:
    """nexus_svc holds pg_monitor MEMBERSHIP (grants-004), but that alone is
    not necessarily usable privilege — cloud is measured NOINHERIT, local
    provisioning currently keeps PG's INHERIT default (nexus-v80f2 tracks
    the divergence). nexus.db.svc_monitor.wal_retention_report is the one
    product-side place the SET-ROLE escalation happens; these tests exercise
    the CLI wiring only (flag registered, dispatch reaches the check,
    measured vs UNMEASURED rendering) via a monkeypatched report function —
    the real escalation mechanism is proven live in
    tests/db/test_svc_monitor_role_live.py.
    """

    def test_flag_dispatches_to_the_check(
        self, runner: CliRunner, monkeypatch,
    ) -> None:
        calls = {"n": 0}

        def _fake_report() -> str:
            calls["n"] += 1
            return "WAL retention (local service): 42 bytes retained"

        monkeypatch.setattr("nexus.db.svc_monitor.wal_retention_report", _fake_report)
        result = runner.invoke(main, ["doctor", "--check-wal-retention"])
        assert result.exit_code == 0, result.output
        assert calls["n"] == 1  # dispatch really reached _run_check_wal_retention

    def test_measured_sample_renders_check_mark_and_exits_zero(
        self, runner: CliRunner, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "nexus.db.svc_monitor.wal_retention_report",
            lambda: "WAL retention (local service): 8388608 bytes retained",
        )
        result = runner.invoke(main, ["doctor", "--check-wal-retention"])
        assert result.exit_code == 0, result.output
        assert "[✓]" in result.output
        assert "WAL retention (local service): 8388608 bytes retained" in result.output

    def test_unmeasured_no_credentials_renders_informational_and_exits_zero(
        self, runner: CliRunner, monkeypatch,
    ) -> None:
        """Absent local nexus_svc credentials (managed/BYO deployment, or a
        pre-provision install) must render UNMEASURED, never a false clean
        — and must NOT fail the check (informational only)."""
        monkeypatch.setattr(
            "nexus.db.svc_monitor.wal_retention_report",
            lambda: (
                "WAL retention: UNMEASURED (no local nexus_svc credentials "
                "-- managed/BYO deployment with nothing local to probe, or "
                "a pre-provision install; this is a local-service-only "
                "sample, not a missing-privilege signal)"
            ),
        )
        result = runner.invoke(main, ["doctor", "--check-wal-retention"])
        assert result.exit_code == 0, result.output
        assert "[ ]" in result.output
        assert "UNMEASURED" in result.output
        assert "[✓]" not in result.output

    def test_unmeasured_set_role_refusal_renders_informational_and_exits_zero(
        self, runner: CliRunner, monkeypatch,
    ) -> None:
        """A SET ROLE refusal (grants-004 never applied) must also degrade
        to UNMEASURED rather than propagating an exception through the CLI
        — the check is informational, never a hard failure."""
        monkeypatch.setattr(
            "nexus.db.svc_monitor.wal_retention_report",
            lambda: (
                "WAL retention: UNMEASURED (svc_monitor: SET ROLE "
                "pg_monitor refused for nexus_svc. nexus_svc must hold "
                "pg_monitor MEMBERSHIP before this call can succeed "
                "(grants-004-monitor-wal-visibility, "
                "service/src/main/resources/db/changelog/"
                "grants-nexus-svc.xml).)"
            ),
        )
        result = runner.invoke(main, ["doctor", "--check-wal-retention"])
        assert result.exit_code == 0, result.output
        assert "[ ]" in result.output
        assert "UNMEASURED" in result.output
        assert "grants-004-monitor-wal-visibility" in result.output


# ── --check-engine-activity (nexus-s71lr) ────────────────────────────────────


class TestCheckEngineActivity:
    """"What is the engine doing right now" -- bead nexus-s71lr deliverable 3.
    Always exit 0 (informational, same posture as --check-wal-retention); the
    real GET /v1/status probe is proven at the http_engine_status unit-test
    layer (tests/test_http_engine_status.py) -- these tests exercise the CLI
    wiring only, via a monkeypatched fetch_engine_status."""

    def test_flag_dispatches_to_the_check(self, runner: CliRunner, monkeypatch) -> None:
        calls = {"n": 0}

        def _fake_fetch(**kwargs):
            calls["n"] += 1
            return {"embedding_mode": "onnx-local", "local_embed_activity": None}

        monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
        result = runner.invoke(main, ["doctor", "--check-engine-activity"])
        assert result.exit_code == 0, result.output
        assert calls["n"] == 1  # dispatch really reached _run_check_engine_activity

    def test_reachable_engine_renders_the_activity_line(
        self, runner: CliRunner, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "nexus.db.http_engine_status.fetch_engine_status",
            lambda **kwargs: {
                "embedding_mode": "onnx-local",
                "local_embed_activity": {
                    "active": True, "chunks_done_total": 128, "sub_batches_total": 8,
                    "last_chunks_per_sec": 7.7, "last_activity_age_ms": 230,
                    "queue_depth": 0, "thread_width": 4,
                },
            },
        )
        result = runner.invoke(main, ["doctor", "--check-engine-activity"])
        assert result.exit_code == 0, result.output
        assert "Engine activity:" in result.output
        assert "chunks_done=128" in result.output

    def test_unreachable_engine_renders_unknown_and_exits_zero(
        self, runner: CliRunner, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "nexus.db.http_engine_status.fetch_engine_status", lambda **kwargs: None,
        )
        result = runner.invoke(main, ["doctor", "--check-engine-activity"])
        assert result.exit_code == 0, result.output
        assert "UNKNOWN" in result.output

    def test_promoted_into_the_default_sweep(self, runner: CliRunner, monkeypatch) -> None:
        calls = {"n": 0}

        def _fake_fetch(**kwargs):
            calls["n"] += 1
            return None

        monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
        runner.invoke(main, ["doctor"])
        assert calls["n"] == 1, "the default sweep must run engine-activity too, not only --check-engine-activity"


# ── --json on the main sweep (nexus-0vycz) ──────────────────────────────────
#
# Prior to this fix, `nx doctor --json` on the main sweep was accepted and
# silently ignored -- it emitted the human-readable report at rc=0. The
# shakedown playbook's S3 signal-density audit needs to classify every
# green's detail string without scraping unicode glyphs, so the main sweep
# must emit one JSON object with a `checks` array (each entry carrying at
# least name/ok/status/detail) plus summary counts.


class TestJsonMainSweep:
    def test_emits_a_single_parseable_json_object(self, runner, mock_reg):
        import json as _json

        result = _invoke(runner, mock_reg, extra_args=["--json"])
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)
        assert isinstance(data, dict)
        assert isinstance(data["checks"], list)
        assert len(data["checks"]) > 0

    def test_stdout_is_json_only_no_human_prose_mixed_in(self, runner, mock_reg):
        """A trailing/leading human line would break a naive `json.loads`
        on the whole stdout -- this is the actual non-vacuous proof that
        nothing besides the JSON document reached stdout."""
        import json as _json

        result = _invoke(runner, mock_reg, extra_args=["--json"])
        _json.loads(result.stdout)  # raises if anything but pure JSON
        assert "Nexus health check:" not in result.stdout
        assert "✓" not in result.stdout

    def test_check_entries_carry_required_fields(self, runner, mock_reg):
        import json as _json

        result = _invoke(runner, mock_reg, extra_args=["--json"])
        data = _json.loads(result.stdout)
        for entry in data["checks"]:
            assert {"name", "ok", "status", "detail"} <= set(entry)
            assert isinstance(entry["name"], str) and entry["name"]
            assert isinstance(entry["ok"], bool)
            assert entry["status"] in ("ok", "warn", "fail")
            assert isinstance(entry["detail"], str)

    def test_summary_counts_match_checks(self, runner, mock_reg):
        import json as _json

        result = _invoke(runner, mock_reg, extra_args=["--json"])
        data = _json.loads(result.stdout)
        checks = data["checks"]
        summary = data["summary"]
        assert summary["total"] == len(checks)
        assert summary["ok"] == sum(1 for c in checks if c["status"] == "ok")
        assert summary["warn"] == sum(1 for c in checks if c["status"] == "warn")
        assert summary["fail"] == sum(1 for c in checks if c["status"] == "fail")
        assert summary["ok"] + summary["warn"] + summary["fail"] == summary["total"]

    def test_all_healthy_exits_zero_and_reports_zero_fails(self, runner, mock_reg):
        import json as _json

        result = _invoke(runner, mock_reg, extra_args=["--json"])
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)
        assert data["summary"]["fail"] == 0

    def test_fatal_failure_reflected_as_fail_status_and_nonzero_exit(
        self, runner, mock_reg,
    ):
        import json as _json

        result = _invoke(runner, mock_reg, extra_args=["--json"], extra_patches=[
            patch("nexus.health._python_ok", return_value=(False, "3.11.0")),
        ])
        # nexus-be6x8 (7.21.0): fatal reds exit 2; hard non-fatal reds exit 1.
        assert result.exit_code == 2, result.output
        data = _json.loads(result.stdout)
        assert data["summary"]["fail"] >= 1
        python_entries = [c for c in data["checks"] if "Python" in c["name"]]
        assert python_entries, data["checks"]
        assert python_entries[0]["status"] == "fail"
        assert python_entries[0]["ok"] is False
        assert "3.11.0" in python_entries[0]["detail"]

    def test_no_json_flag_human_output_is_unchanged(self, runner, mock_reg):
        """Regression guard: human output stays byte-identical to the
        pre-fix format when --json is absent."""
        import json as _json

        result = _invoke(runner, mock_reg)
        assert result.exit_code == 0
        assert "Nexus health check:\n" in result.output
        assert "✓" in result.output
        with pytest.raises(_json.JSONDecodeError):
            _json.loads(result.output)


class TestJsonUnsupportedCombinations:
    """Every OTHER doctor mode combined with --json must fail loud instead
    of silently ignoring the flag (nexus-0vycz). --check-search,
    --check-quotas, and --check-mcp-logs already honor --json and are
    covered by their own test classes/files -- not repeated here."""

    @pytest.mark.parametrize("flag", [
        "--check-schema",
        "--check-resources",
        "--check-taxonomy",
        "--check-plan-library",
        "--fix",
        "--clean-checkpoints",
        "--clean-pipelines",
        "--fix-paths",
        "--check-post-store-hooks",
        "--check-mineru",
        "--check-aspect-queue",
        "--check-t1",
        "--check-wal-retention",
        "--check-tier-discipline",
        "--check-storage-boundary",
        "--trim-telemetry",
    ])
    def test_json_with_unsupported_mode_is_a_loud_error(self, runner, mock_reg, flag):
        result = _invoke(runner, mock_reg, extra_args=[flag, "--json"])
        assert result.exit_code == 2, result.output
        assert flag in result.output


class TestJsonUnsupportedModesExhaustive:
    """nexus-0vycz round 2 (substantive-critic Significant, 2026-08-17):
    ``_json_unsupported_modes`` (doctor.py) and the parametrize list
    above are two INDEPENDENTLY hand-maintained enumerations of the
    same 16 flags -- neither is derived from ``doctor_cmd``'s own
    parameters. A 17th ``--check-X`` flag added later without a
    matching ``_json_unsupported_modes`` entry silently reproduces the
    ORIGINAL nexus-0vycz bug (--json combined with the new mode is
    silently ignored instead of a usage error) with nothing here to
    catch it.

    Modeled on ``tests/test_catalog_doctor_new_checks.py::TestUsage::
    test_no_flag_lists_every_check``, which fixed the identical
    anti-pattern for a sibling command's usage-error surface: derive
    the "should be in the dict" set from the command's own structure
    instead of trusting a second hand-written list to stay in sync.

    Structural criterion for "a mode flag --json cannot combine with,
    besides the three that already support it": an ``if <param>:``
    block, directly in ``doctor_cmd``'s body, whose body contains a
    ``return`` -- i.e. a flag that unconditionally early-returns before
    reaching the main health sweep. Every existing mode flag
    (--check-schema, --fix, --clean-checkpoints, ...) follows this
    exact shape; --check-search/--check-quotas/--check-mcp-logs follow
    it too but are excluded (they DO support --json).
    """

    _JSON_SUPPORTED_MODE_PARAMS = frozenset({
        "check_search", "check_quotas", "check_mcp_logs",
    })

    # nexus-omdjz: the AST early-return criterion only recognizes a mode
    # flag shaped as a bare top-level ``if <param>: ... return``. A mode
    # flag implemented some other way (e.g. gated behind a helper call --
    # ``if _maybe_run_mode(check_x): return`` -- whose ``ast.If.test`` is
    # a ``Call``, not a ``Name``) would silently fall through
    # ``_early_return_flag_params`` uncounted: not flagged as a mode, not
    # caught by ``test_dict_covers_exactly_every_early_return_mode_flag``
    # (which only diffs against the recognized set), and the criterion
    # would report a false "not a mode" without anyone noticing.
    #
    # ``_SWEEP_COMPATIBLE_MODE_PARAMS`` is the explicit, hand-verified
    # complement: every ``is_flag`` doctor_cmd parameter that is NOT an
    # early-return mode, confirmed by reading doctor_cmd's body directly.
    # Each name below reaches the final health-sweep branch (or modifies
    # another mode's behavior) without ever early-returning on its own:
    #   - json_out: toggles JSON formatting; gates a UsageError check but
    #     never itself ``return``s.
    #   - dry_run: read inside the ``fix_paths`` mode's own body to choose
    #     between reporting and writing; has no ``if dry_run:`` of its own.
    #   - fail_on_violation: read inside the ``check_storage_boundary``
    #     mode's own body as an argument; has no ``if fail_on_violation:``
    #     of its own.
    # ``test_recognized_modes_plus_sweep_compatible_covers_every_flag``
    # below is the fail-loud floor: a new is_flag parameter that lands in
    # NEITHER this set NOR the AST-recognized set fails explicitly, naming
    # the parameter, instead of silently passing as an unclassified flag.
    _SWEEP_COMPATIBLE_MODE_PARAMS = frozenset({
        "json_out", "dry_run", "fail_on_violation",
    })

    @staticmethod
    def _doctor_cmd_ast():
        import ast
        import inspect
        import textwrap
        from nexus.commands.doctor import doctor_cmd

        src = textwrap.dedent(inspect.getsource(doctor_cmd.callback))
        return ast.parse(src).body[0]

    @classmethod
    def _early_return_flag_params(cls) -> set[str]:
        func = cls._doctor_cmd_ast()
        param_names = {a.arg for a in func.args.args}
        return {
            node.test.id
            for node in func.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id in param_names
            and any(isinstance(n, ast.Return) for n in node.body)
        }

    @classmethod
    def _json_unsupported_modes_dict_from_source(cls) -> dict[str, str]:
        """Statically parse the ``_json_unsupported_modes = {...}`` dict
        literal out of ``doctor_cmd``'s source -- flag string -> param
        name. Pure introspection, no invocation."""
        func = cls._doctor_cmd_ast()
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_json_unsupported_modes"
            ):
                return {
                    k.value: v.id
                    for k, v in zip(node.value.keys, node.value.values)
                }
        raise AssertionError(
            "could not find `_json_unsupported_modes = {...}` in "
            "doctor_cmd's source -- this guard's static-parse assumption "
            "no longer holds; update "
            "_json_unsupported_modes_dict_from_source to match the new shape"
        )

    def test_dict_covers_exactly_every_early_return_mode_flag(self):
        early_return_params = self._early_return_flag_params()
        expected_params = early_return_params - self._JSON_SUPPORTED_MODE_PARAMS

        actual_params = set(
            self._json_unsupported_modes_dict_from_source().values()
        )

        missing = expected_params - actual_params
        extra = actual_params - expected_params
        assert not missing and not extra, (
            "doctor_cmd's _json_unsupported_modes dict has drifted from "
            f"its own early-return mode flags (missing={sorted(missing)}, "
            f"extra={sorted(extra)}). A new --check-X mode flag must be "
            "added to BOTH _json_unsupported_modes (doctor.py) AND "
            "TestJsonUnsupportedCombinations's parametrize list above -- "
            "skipping the dict silently lets --json combine with the new "
            "mode without a usage error (the original nexus-0vycz bug)."
        )

    @staticmethod
    def _all_is_flag_param_names() -> set[str]:
        from nexus.commands.doctor import doctor_cmd

        return {
            p.name for p in doctor_cmd.params if getattr(p, "is_flag", False)
        }

    def test_recognized_modes_plus_sweep_compatible_covers_every_flag(self):
        """Fail-loud floor (nexus-omdjz): every is_flag doctor_cmd param
        must be classified as EITHER a recognized early-return mode flag
        OR a declared sweep-compatible modifier -- never neither. A flag
        the AST criterion cannot recognize (e.g. a mode implemented via a
        helper-call gate instead of a bare ``if <param>:``) is invisible
        to ``test_dict_covers_exactly_every_early_return_mode_flag`` by
        construction; this test is the independent check that catches it
        by comparing against doctor_cmd's actual parameter list instead
        of only against the AST's own recognized set.
        """
        all_flags = self._all_is_flag_param_names()
        recognized = self._early_return_flag_params()
        sweep_compatible = self._SWEEP_COMPATIBLE_MODE_PARAMS

        unclassified = all_flags - recognized - sweep_compatible
        assert not unclassified, (
            f"doctor_cmd has is_flag parameter(s) {sorted(unclassified)} "
            "that this test cannot classify: not recognized as an "
            "early-return mode flag (the AST scan found no bare "
            "`if <param>: ... return`), and not declared in "
            "_SWEEP_COMPATIBLE_MODE_PARAMS. Classify it explicitly -- "
            "add it to _json_unsupported_modes (doctor.py) if it is a "
            "mode flag (whatever its early-return shape), or to "
            "_SWEEP_COMPATIBLE_MODE_PARAMS above if it is a modifier "
            "that legitimately reaches the main health sweep without "
            "early-returning on its own."
        )

        overlap = recognized & sweep_compatible
        assert not overlap, (
            f"parameter(s) {sorted(overlap)} are classified as BOTH an "
            "early-return mode flag and sweep-compatible -- a flag "
            "cannot be both; fix _SWEEP_COMPATIBLE_MODE_PARAMS."
        )

        stale = sweep_compatible - all_flags
        assert not stale, (
            f"_SWEEP_COMPATIBLE_MODE_PARAMS names {sorted(stale)} which "
            "are no longer is_flag parameters of doctor_cmd -- stale "
            "entry, remove it."
        )


class TestCheckDanglingLinksFlagIsRetired:
    """RDR-194 P7 deliverable 1 (bead nexus-tk070.p7): pin the ABSENCE of
    the retired ``--check-dangling-links`` / ``--strict-dangling-links``
    flags, not just their absence from ``doctor_cmd``'s
    ``@click.option`` decorators (verified by code inspection instead --
    ``grep -rn 'check.dangling.links' src/`` finds nothing but the
    HttpCatalogClient.orphaned_links() docstring explaining the
    retirement, nexus-tk070.p1 / RDR-194 § D2, catalog-032-links-
    tumbler-fk.xml). Both flags were retired in the SAME commit
    (b948bee3a) that VALIDATEs ``fk_catalog_links_from_document`` /
    ``fk_catalog_links_to_document`` (D0.10 one-for-one) and deleted
    ``tests/test_doctor_dangling_links.py`` outright -- this test is the
    one that survives to prove a REGRESSION (someone re-adding the flag,
    or a future doctor.py rewrite accidentally reviving it) would be
    caught, since the deleted file obviously cannot catch its own
    resurrection.

    Click's own usage-error path is the assertion: an unrecognized
    option exits 2 with "no such option" on stderr/output, never a
    silent no-op and never a crash. No mock_reg / engine substrate
    needed -- click never reaches ``doctor_cmd``'s body for an unknown
    option.
    """

    @pytest.mark.parametrize("flag", ["--check-dangling-links", "--strict-dangling-links"])
    def test_flag_is_unrecognized(self, runner: CliRunner, flag: str) -> None:
        result = runner.invoke(main, ["doctor", flag])
        assert result.exit_code == 2, (
            f"expected {flag} to be an unrecognized option (exit 2), got "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert "no such option" in result.output.lower(), (
            f"expected a 'no such option' usage error for {flag}, got: {result.output!r}"
        )
