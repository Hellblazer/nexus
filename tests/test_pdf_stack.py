# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PDF stack on demand. nexus-2k66t.4.2.

docling, MinerU, torch, torchvision and pdftext are the ``pdf`` extra. A fresh
install goes without them; the first PDF index installs them; nothing ever
falls back quietly (nexus-2fyb). These tests pin the state machine in
``nexus.pdf_stack`` against fakes (no generation is built, no network), the
exit codes, the loop guard and the policy. The entry-point wiring, the upgrade
rule, the doctor row and the real CLI under a hidden stack have their own files.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import click
import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from nexus import pdf_stack
from tests._module_seam import setattr_in
from nexus.pdf_stack import (
    Outcome,
    PdfStackInstallError,
    PdfStackMissingError,
    PdfStackNetworkError,
    StackState,
)

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(pdf_stack.POLICY_ENV, raising=False)
    monkeypatch.delenv(pdf_stack.REEXEC_ENV, raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    pdf_stack.reset_cache()


def _state(status: str, detail: str = "") -> StackState:
    return StackState(status, detail)


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch):
    """A controllable stack: ``stack.status`` drives ``state()``; the installer
    and the install plan are fakes that record what was asked of them."""

    class _Stack:
        status = "absent"
        detail = "docling, MinerU and torch are not installed"
        plan = pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND)
        installed = 0
        install_error: BaseException | None = None
        flip_present_on_install = False
        windows_missing = False

    s = _Stack()

    def _fake_state() -> StackState:
        return _state(s.status, s.detail)

    def _fake_install(*, carrying: bool = False) -> None:
        s.installed += 1
        if s.install_error is not None:
            raise s.install_error
        if s.flip_present_on_install:
            s.status = "present"

    monkeypatch.setattr(pdf_stack, "state", _fake_state)
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: s.plan)
    monkeypatch.setattr(pdf_stack, "_install_generation", _fake_install)
    monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: s.windows_missing)
    return s


# ── what the extra holds ─────────────────────────────────────────────────────


def _pyproject() -> dict:
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_pdf_extra_holds_exactly_the_stack_and_the_core_holds_none_of_it() -> None:
    data = _pyproject()
    names = lambda reqs: {canonicalize_name(Requirement(r).name) for r in reqs}  # noqa: E731
    extra = names(data["project"]["optional-dependencies"]["pdf"])
    assert extra == {canonicalize_name(d) for d in pdf_stack.STACK_DISTS}, extra
    core = names(data["project"]["dependencies"])
    assert not (core & extra), f"the slim install would still carry {sorted(core & extra)}"
    # pymupdf stays: the formula screen and the doc indexer use it.
    assert "pymupdf" in core
    # The suite, CI and `uv sync` keep the stack through the dev group.
    assert "conexus[pdf]" in data["dependency-groups"]["dev"]


def test_the_probe_modules_are_the_import_names_of_the_stack_dists() -> None:
    assert len(pdf_stack.STACK_MODULES) == len(pdf_stack.STACK_DISTS)
    assert pdf_stack.STACK_MODULES[0] == "docling"  # docling-slim ships `docling`


# ── state() ──────────────────────────────────────────────────────────────────


def test_the_dev_environment_has_the_stack_on_its_pins() -> None:
    st = pdf_stack.state()
    assert st.present, (st.status, st.detail)


def test_pins_read_from_the_shipped_constraints_keep_only_this_platforms_line() -> None:
    pins = pdf_stack._pins(pdf_stack.STACK_DISTS)
    assert set(pins) == set(pdf_stack.STACK_DISTS), pins
    # torch has three marker lines in the file; exactly one holds here.
    assert pins["torch"] in ("2.8.0", "2.10.0")
    assert "+" not in pins["torch"], "the +cpu label is dropped from the pins"


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch):
    """Fake ``find_spec`` and ``importlib.metadata`` for the five modules."""

    class _P:
        found = set(pdf_stack.STACK_MODULES)
        versions: dict[str, str | None] = {}
        receipt_pdf = False

    p = _P()
    pins = pdf_stack._pins(pdf_stack.STACK_DISTS)
    p.versions = dict(pins)
    monkeypatch.setattr(pdf_stack, "_find_spec", lambda name: name in p.found)
    monkeypatch.setattr(pdf_stack, "_dist_version", lambda name: p.versions.get(name))
    monkeypatch.setattr(pdf_stack, "_receipt_lists_pdf", lambda: p.receipt_pdf)
    return p


def test_state_present_when_every_module_resolves_and_every_version_is_on_its_pin(probes) -> None:
    assert pdf_stack.state().present


def test_a_local_version_label_does_not_make_a_pin_mismatch(probes) -> None:
    probes.versions["torch"] = probes.versions["torch"] + "+cpu"
    assert pdf_stack.state().present


def test_state_absent_when_none_of_it_is_there(probes) -> None:
    probes.found = set()
    st = pdf_stack.state()
    assert st.status == "absent", st


def test_state_broken_when_the_receipt_lists_pdf_over_an_empty_tree(probes) -> None:
    probes.found = set()
    probes.receipt_pdf = True
    assert pdf_stack.state().status == "broken"


def test_state_broken_when_one_module_is_missing(probes) -> None:
    probes.found = set(pdf_stack.STACK_MODULES) - {"mineru"}
    st = pdf_stack.state()
    assert st.status == "broken" and "mineru" in st.detail


def test_state_broken_when_a_version_is_off_its_pin(probes) -> None:
    probes.versions["mineru"] = "9.9.9"
    st = pdf_stack.state()
    assert st.status == "broken" and "mineru" in st.detail and "9.9.9" in st.detail


def test_state_broken_when_a_distribution_is_not_installed(probes) -> None:
    probes.versions["pdftext"] = None
    assert pdf_stack.state().status == "broken"


def test_state_never_imports_torch() -> None:
    """The reason state() exists as find_spec plus metadata: a doctor row or a
    refusal must not pay torch's import (seconds, hundreds of MB resident)."""
    code = (
        "import sys\n"
        "from nexus import pdf_stack\n"
        "st = pdf_stack.state()\n"
        "assert st.status in ('present', 'absent', 'broken'), st\n"
        "heavy = sorted(m for m in ('torch', 'torchvision', 'docling', 'mineru') if m in sys.modules)\n"
        "assert not heavy, heavy\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


# ── policy ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [
    ("install", "install"), ("refuse", "refuse"), (" REFUSE ", "refuse"), ("Install", "install"),
])
def test_policy_reads_the_environment(monkeypatch, value, expected) -> None:
    monkeypatch.setenv(pdf_stack.POLICY_ENV, value)
    assert pdf_stack.policy() == expected


def test_policy_defaults_to_install() -> None:
    assert pdf_stack.policy() == "install"


def test_an_unreadable_policy_refuses_rather_than_starting_a_download(monkeypatch) -> None:
    monkeypatch.setenv(pdf_stack.POLICY_ENV, "yes-please")
    assert pdf_stack.policy() == "refuse"


@pytest.mark.parametrize("configured, expected", [
    (False, "refuse"), ("false", "refuse"), ("off", "refuse"), (True, "install"), ("true", "install"),
])
def test_policy_reads_the_config_key_and_the_environment_wins(monkeypatch, configured, expected) -> None:
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {"pdf_stack": {"auto_install": configured}})
    assert pdf_stack.policy() == expected
    monkeypatch.setenv(pdf_stack.POLICY_ENV, "install" if expected == "refuse" else "refuse")
    assert pdf_stack.policy() == ("install" if expected == "refuse" else "refuse")


# ── require() ────────────────────────────────────────────────────────────────


def test_require_passes_when_present_and_caches_the_verdict(stack) -> None:
    stack.status = "present"
    pdf_stack.require("x")
    stack.status = "absent"  # a cached positive is not re-asked within the process
    pdf_stack.require("x")


def test_require_refuses_by_name_and_never_installs(stack) -> None:
    with pytest.raises(PdfStackMissingError) as err:
        pdf_stack.require("extracting a.pdf")
    msg = str(err.value)
    assert "extracting a.pdf" in msg and pdf_stack.INSTALL_COMMAND in msg
    assert "nothing was written" in msg
    assert stack.installed == 0
    assert isinstance(err.value, RuntimeError)
    assert err.value.exit_code == 69


# ── ensure(): the state machine ──────────────────────────────────────────────


def test_ensure_present_goes_on_without_installing(stack) -> None:
    stack.status = "present"
    assert pdf_stack.ensure("nx index pdf") is Outcome.PRESENT
    assert stack.installed == 0


def test_ensure_absent_installs_once_and_asks_the_caller_to_rerun(stack) -> None:
    assert pdf_stack.ensure("nx index pdf") is Outcome.INSTALLED
    assert stack.installed == 1


def test_ensure_refuse_policy_exits_69_with_the_command_and_installs_nothing(stack, monkeypatch) -> None:
    monkeypatch.setenv(pdf_stack.POLICY_ENV, "refuse")
    with pytest.raises(PdfStackMissingError) as err:
        pdf_stack.ensure("nx index repo")
    assert err.value.exit_code == 69
    assert pdf_stack.INSTALL_COMMAND in str(err.value)
    assert "NX_PDF_STACK=refuse" in str(err.value)
    assert stack.installed == 0


@pytest.mark.parametrize("kind, command", [
    ("directory", "NX_REINSTALL_EXTRAS=pdf scripts/reinstall-tool.sh"),
    ("legacy", "nx self install && nx self install --extras pdf"),
    ("dev", "uv sync"),
])
def test_ensure_refuses_a_layout_the_auto_path_does_not_touch(stack, kind, command) -> None:
    stack.plan = pdf_stack.InstallPlan(kind, False, command, "because of the layout")
    with pytest.raises(PdfStackMissingError) as err:
        pdf_stack.ensure("nx index pdf")
    assert command in str(err.value) and "Because of the layout" in str(err.value)
    assert err.value.exit_code == 69
    assert stack.installed == 0


def test_ensure_a_broken_stack_is_repaired_through_the_same_path(stack) -> None:
    stack.status, stack.detail = "broken", "mineru: 9.9.9, the pin is 3.1.11"
    assert pdf_stack.ensure("nx index pdf") is Outcome.INSTALLED
    assert stack.installed == 1


def test_a_broken_stack_under_refuse_names_what_is_wrong(stack, monkeypatch) -> None:
    monkeypatch.setenv(pdf_stack.POLICY_ENV, "refuse")
    stack.status, stack.detail = "broken", "mineru: 9.9.9, the pin is 3.1.11"
    with pytest.raises(PdfStackMissingError, match="broken.*mineru: 9.9.9"):
        pdf_stack.ensure("nx index pdf")


def test_ensure_refuses_on_windows_without_the_vc_runtime_before_downloading(stack) -> None:
    stack.windows_missing = True
    with pytest.raises(PdfStackMissingError) as err:
        pdf_stack.ensure("nx index pdf")
    assert pdf_stack.VCRT_URL in str(err.value)
    assert err.value.exit_code == 69
    assert stack.installed == 0


# A peer that installed while we waited is pinned in
# tests/test_pdf_stack_current_generation.py against two real generations on disk:
# state() reads the running interpreter and cannot see a peer's generation, so a
# test that flips state() between two calls tests a transition no producer makes.


@pytest.mark.parametrize("text, cls, code", [
    ("generation build failed:\nerror: Failed to fetch: dns error: failed to lookup address", PdfStackNetworkError, 75),
    ("generation build failed:\nrequest failed after 3 retries: timed out", PdfStackNetworkError, 75),
    ("generation build failed:\ninvalid peer certificate: UnknownIssuer", PdfStackNetworkError, 75),
    # uv 0.8 under UV_OFFLINE (measured by the review of cb36b89de): the old regexes read these as 70
    ("generation build failed:\nerror: Failed to fetch: x\n  Caused by: Packages were unavailable because the network was disabled", PdfStackNetworkError, 75),
    ("generation build failed:\nNetwork connectivity is disabled, but the requested data wasn't found in the cache", PdfStackNetworkError, 75),
    ("generation build failed:\nNo solution found when resolving dependencies", PdfStackInstallError, 70),
    ("generation build failed:\nNo space left on device", PdfStackInstallError, 70),
])
def test_installer_failures_map_to_75_for_the_network_and_70_for_the_rest(stack, text, cls, code) -> None:
    stack.install_error = click.ClickException(text)
    with pytest.raises(cls) as err:
        pdf_stack.ensure("nx index pdf")
    assert err.value.exit_code == code
    assert isinstance(err.value.__cause__, click.ClickException)
    assert "nothing was written" in str(err.value)
    if code == 70:
        assert text.splitlines()[-1] in str(err.value), "the tail of uv's output is in the message"


def test_the_loop_guard_turns_a_still_absent_stack_into_exit_70(stack, monkeypatch) -> None:
    """The re-run child sets NX_PDF_STACK_REEXEC=1. If it STILL cannot see the
    stack, installing again would loop forever; it is the 'reported success,
    still absent' failure."""
    monkeypatch.setenv(pdf_stack.REEXEC_ENV, "1")
    with pytest.raises(PdfStackInstallError) as err:
        pdf_stack.ensure("nx index pdf")
    assert err.value.exit_code == 70
    assert stack.installed == 0
    assert "still reports it absent" in str(err.value)


def test_the_loop_guard_is_inert_when_the_stack_is_there(stack, monkeypatch) -> None:
    monkeypatch.setenv(pdf_stack.REEXEC_ENV, "1")
    stack.status = "present"
    assert pdf_stack.ensure("nx index pdf") is Outcome.PRESENT


# ── ensure_or_exit(): what a CLI entry point does with it ────────────────────


def test_ensure_or_exit_returns_true_when_present(stack) -> None:
    stack.status = "present"
    assert pdf_stack.ensure_or_exit("nx index pdf") is True


@pytest.mark.parametrize("setup, code", [
    ("refuse", 69), ("network", 75), ("installer", 70),
])
def test_ensure_or_exit_exits_with_the_error_class_status(stack, monkeypatch, capsys, setup, code) -> None:
    if setup == "refuse":
        monkeypatch.setenv(pdf_stack.POLICY_ENV, "refuse")
    elif setup == "network":
        stack.install_error = click.ClickException("dns error")
    else:
        stack.install_error = click.ClickException("No solution found")
    with pytest.raises(SystemExit) as exit_:
        pdf_stack.ensure_or_exit("nx index pdf")
    assert exit_.value.code == code
    err = capsys.readouterr().err
    assert err.startswith("nx: ") and "nothing was written" in err


def test_ensure_or_exit_reruns_in_the_new_generation_and_exits_with_the_childs_status(stack, monkeypatch) -> None:
    seen: list[list[str] | None] = []
    monkeypatch.setattr(pdf_stack, "rerun_command", lambda argv=None: (seen.append(argv), 7)[1])
    with pytest.raises(SystemExit) as exit_:
        pdf_stack.ensure_or_exit("nx index pdf")
    assert exit_.value.code == 7 and len(seen) == 1 and stack.installed == 1


def test_defer_rerun_unwinds_instead_of_running_the_child_under_a_caller_lock(stack, monkeypatch) -> None:
    monkeypatch.setattr(pdf_stack, "rerun_command", lambda argv=None: pytest.fail("must not rerun here"))
    with pytest.raises(pdf_stack.RerunRequired):
        pdf_stack.ensure_or_exit("nx index repo", defer_rerun=True)


def test_rerun_required_is_not_swallowed_by_a_broad_except_exception() -> None:
    assert not issubclass(pdf_stack.RerunRequired, Exception)
    try:
        try:
            raise pdf_stack.RerunRequired()
        except Exception:  # noqa: BLE001 - the containment the indexer has between the gate and the CLI
            pytest.fail("RerunRequired was swallowed")
    except pdf_stack.RerunRequired:
        pass


def test_hold_on_refuse_returns_false_but_a_network_failure_still_exits(stack, monkeypatch, capsys) -> None:
    monkeypatch.setenv(pdf_stack.POLICY_ENV, "refuse")
    assert pdf_stack.ensure_or_exit("nx index repo", hold_on_refuse=True) is False
    assert "PDF support is not installed" in capsys.readouterr().err
    monkeypatch.delenv(pdf_stack.POLICY_ENV)
    stack.install_error = click.ClickException("dns error")
    with pytest.raises(SystemExit) as exit_:
        pdf_stack.ensure_or_exit("nx index repo", hold_on_refuse=True)
    assert exit_.value.code == 75


# ── rerun_command() ──────────────────────────────────────────────────────────


def test_rerun_runs_the_new_generations_nx_with_the_same_argv_and_the_loop_guard(monkeypatch, tmp_path) -> None:
    gen = tmp_path / "gen-new"
    (gen / "bin").mkdir(parents=True)
    monkeypatch.setattr("nexus.install_layout.current_generation", lambda *a, **k: gen)
    ran: dict = {}

    def _run(argv, env=None, check=None):
        ran.update(argv=argv, env=env)
        return subprocess.CompletedProcess(argv, 3)

    setattr_in(monkeypatch, ("nexus.pdf_stack",), "subprocess.run", _run)
    assert pdf_stack.rerun_command(["index", "pdf", "a.pdf"]) == 3
    assert ran["argv"] == [str(gen / "bin" / "nx"), "index", "pdf", "a.pdf"]
    assert ran["env"][pdf_stack.REEXEC_ENV] == "1"


# ── the install itself: one helper, the generation builder's own arguments ───


def test_the_install_goes_through_perform_self_install_with_the_pdf_extra_and_the_running_version(monkeypatch, capsys) -> None:
    """Never a bare `uv pip install conexus[pdf]` (CUDA torch on Linux), and the
    version pin is load-bearing: without it the fetch is an upgrade."""
    from nexus.commands import self_cmd

    calls: list[dict] = []
    monkeypatch.setattr(self_cmd, "perform_self_install", lambda **kw: calls.append(kw))
    monkeypatch.setattr(pdf_stack, "_running_version", lambda: "9.8.7")
    pdf_stack._install_generation()
    assert calls == [{"add_extras": ("pdf",), "version": "9.8.7", "progress": True}]
    err = capsys.readouterr().err
    assert "Installing PDF support for conexus 9.8.7" in err
    assert "370 MB" in err and "kept by future upgrades" in err


# ── the PDFs a refusing run left behind ──────────────────────────────────────


def test_skipped_record_round_trips_and_counts_per_repo(tmp_path) -> None:
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    assert pdf_stack.skipped_count() == 0
    pdf_stack.record_skipped([repo_a / "x.pdf", repo_a / "y.pdf"], repo=repo_a)
    pdf_stack.record_skipped([repo_b / "z.pdf"], repo=repo_b)
    assert pdf_stack.skipped_count() == 3
    pdf_stack.record_skipped([], repo=repo_a)  # a later run indexed them
    assert pdf_stack.skipped_count() == 1
    pdf_stack.record_skipped([], repo=repo_b)
    assert pdf_stack.skipped_count() == 0
    assert not (tmp_path / "cfg" / "pdf-stack-skipped.json").exists()
