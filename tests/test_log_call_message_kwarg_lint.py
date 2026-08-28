# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0idx (+ extension sweep): repo-wide lint closing the whole class.

``_log.<level>(..., message=...)`` raises ``KeyError("Attempt to overwrite
'message' in LogRecord")`` the moment structlog is configured to render
through stdlib logging (``structlog.stdlib.render_to_log_kwargs`` +
``LoggerFactory``) — production configs and any caplog-based test that
routes through stdlib do exactly this. ``message`` is a reserved
``LogRecord`` attribute (set internally by ``getMessage()``); passing it
as an extra kwarg is checked and rejected in stdlib's own
``Logger.makeRecord``, before any formatting happens.

``nexus-z0idx`` fixed the one call site it was filed for
(``http_vector_client.py``'s ``_warn_skip_existing_deprecated``) and asked
for a sweep-check: "grep the tree for other structlog calls passing
message= ... pin whatever the sweep finds." The sweep found twelve more
live call sites across six files (``hooks.py`` x3, ``indexer.py`` x4 —
one more than the original three-site estimate, ``corpus.py`` x3,
``pdf_extractor.py`` x1, ``mcp/_first_run.py`` x1 — ``daemon/installer.py``
was in the original grep's raw hit list but both of its ``message=``
occurrences turned out to be ``DaemonUninstallReport(message=...)``
dataclass-field construction, not a logging call, so it was never a
live instance of this bug). All nine were fixed by renaming the kwarg
to whichever name is ALREADY that file's established convention for
"free-text explanation attached to a structured log event" — ``detail=``
in ``hooks.py``, ``corpus.py``, and ``mcp/_first_run.py`` (matching
``http_vector_client.py``'s own pre-existing sibling helpers, where no
counter-convention exists), and ``reason=`` in ``indexer.py`` and
``pdf_extractor.py`` (each already had 6+ pre-existing ``_log`` calls
using ``reason=`` for this exact semantic; introducing ``detail=`` there
instead would have planted a second, coexisting name for the same
concept in the same file — a new inconsistency this fix has no business
creating). The kwarg name itself is arbitrary to the bug (anything but
the reserved ``message`` closes it); which one to pick in each file is
not.

This lint is the actual class closure, not the fix count: a bare
tree-scan proves nothing once the known instances are gone (the
nexus-moht0 vacuous-gate doctrine — see ``TestDetectorSelfFalsifies``
below, which injects a violation into a real file, asserts the scan
goes RED, then reverts). The scope is deliberately narrow and literal
— ``_log.<level>(...)`` / ``log.<level>(...)`` calls only, matching
every logger this codebase actually declares (grep-verified: every
module-level logger in ``src/nexus`` is named ``_log`` or ``log``,
never ``logger`` or a ``self.``-qualified attribute). An inline
``structlog.get_logger().warning(...)`` call (receiver is a Call node,
not a Name) is out of scope for the same reason the coordinator's ask
named ``_log.``/``log.`` specifically: nothing in this tree logs that
way today.
"""
from __future__ import annotations

import ast
import functools
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: Logger method names this lint inspects. Not every one of these fires a
#: real log record (``exception`` does; ``msg``/``log`` are the generic
#: forms), but including a name that is never actually called anywhere
#: costs nothing and closes the class rather than the observed instances.
_LOG_METHOD_NAMES = frozenset({
    "debug", "info", "warning", "warn", "error",
    "critical", "exception", "log", "msg",
})

#: Bare receiver names this lint treats as "a structlog logger" — the
#: ONLY two names any module-level logger in this codebase is bound to
#: (grep-verified against every ``_log = structlog.get_logger(...)`` /
#: ``log = structlog.get_logger(...)`` assignment in ``src/nexus``).
_LOGGER_RECEIVER_NAMES = frozenset({"_log", "log"})


def _receiver_name(node: ast.expr) -> str | None:
    """The bare name a call's receiver resolves to, for the two shapes
    this codebase actually uses: ``_log.warning(...)`` (``ast.Name``) and
    ``self._log.warning(...)`` (``ast.Attribute``). Anything else (a
    call expression receiver, a subscript, ...) returns ``None`` and is
    not scanned — see module docstring for why that is in-scope-by-
    design rather than a gap.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def find_message_kwarg_violations(source: str, filename: str) -> list[str]:
    """Return ``"filename:lineno"`` for every ``_log``/``log`` method call
    in *source* that passes a ``message=`` keyword argument.

    Parse errors propagate — a file this cannot parse is a lint failure
    in its own right, not a reason to silently skip it.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in _LOG_METHOD_NAMES:
            continue
        if _receiver_name(func.value) not in _LOGGER_RECEIVER_NAMES:
            continue
        for kw in node.keywords:
            if kw.arg == "message":
                violations.append(f"{filename}:{node.lineno}")
    return violations


@functools.cache
def _scan_repo() -> tuple[str, ...]:
    """Every violation across ``src/nexus``, cached for the module's tests."""
    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        source = path.read_text()
        if "message=" not in source:
            continue  # cheap pre-filter; the AST walk is the real check
        rel = str(path.relative_to(REPO_ROOT))
        violations.extend(find_message_kwarg_violations(source, rel))
    return tuple(violations)


# ── Detector unit tests (prove the matcher, not just the tree) ─────────────


class TestDetectorMatchesTheRealShape:
    def test_flags_bare_log_call(self) -> None:
        assert find_message_kwarg_violations(
            "_log.warning('evt', message='boom')", "<t>",
        ) != []

    def test_flags_self_qualified_log_call(self) -> None:
        assert find_message_kwarg_violations(
            "self._log.error('evt', message='boom')", "<t>",
        ) != []

    def test_flags_log_named_logger_too(self) -> None:
        assert find_message_kwarg_violations(
            "log.info('evt', message='boom')", "<t>",
        ) != []

    def test_flags_message_kwarg_regardless_of_position(self) -> None:
        assert find_message_kwarg_violations(
            "_log.debug('evt', other=1, message='boom', another=2)", "<t>",
        ) != []

    def test_reports_the_correct_line_number(self) -> None:
        src = "x = 1\n_log.warning('evt', message='boom')\n"
        violations = find_message_kwarg_violations(src, "<t>")
        assert violations == ["<t>:2"]


class TestDetectorNegativeControls:
    """Non-vacuity: a matcher that flagged every call would pass every
    positive test above too. These prove it does NOT."""

    def test_does_not_flag_detail_kwarg(self) -> None:
        assert find_message_kwarg_violations(
            "_log.warning('evt', detail='boom')", "<t>",
        ) == []

    def test_does_not_flag_message_on_a_non_logger_receiver(self) -> None:
        # Same call SHAPE, different receiver name — must not fire on
        # every message= in the tree (warnings.filterwarnings, a
        # dataclass constructor, etc.).
        assert find_message_kwarg_violations(
            "warnings.filterwarnings('ignore', message='x')", "<t>",
        ) == []

    def test_does_not_flag_message_on_a_dataclass_constructor(self) -> None:
        assert find_message_kwarg_violations(
            "DaemonUninstallReport(confirmed=True, message='done')", "<t>",
        ) == []

    def test_does_not_flag_a_log_call_with_no_message_kwarg(self) -> None:
        assert find_message_kwarg_violations(
            "_log.warning('evt', detail='fine', count=3)", "<t>",
        ) == []

    def test_does_not_flag_an_inline_get_logger_call(self) -> None:
        # Receiver is a Call node (structlog.get_logger()), not a Name/
        # Attribute resolving to _log/log — out of scope by design, see
        # module docstring. Pinning this as a negative control rather
        # than silently relying on it.
        assert find_message_kwarg_violations(
            "structlog.get_logger().warning('evt', message='boom')", "<t>",
        ) == []


class TestDetectorSelfFalsifies:
    """nexus-moht0 vacuous-gate doctrine: a scan with zero live hits proves
    nothing about a matcher that never matches anything. Inject a real
    violation into a real source file, assert the repo-wide scan goes
    RED, then revert — proving the scan is actually wired to the tree it
    claims to check, not just to synthetic strings above."""

    def test_injected_violation_in_a_real_file_is_caught(self) -> None:
        target = SRC_ROOT / "corpus.py"
        original = target.read_text()
        assert "message=" not in original, (
            "fixture assumption violated: corpus.py already contains "
            "message= post-fix; this test can no longer inject a clean "
            "violation to prove the scan fires"
        )
        poisoned = original + (
            "\n\ndef _z0idx_lint_self_falsify_probe() -> None:\n"
            "    _log.warning('probe', message='should be caught')\n"
        )
        try:
            target.write_text(poisoned)
            _scan_repo.cache_clear()
            violations = _scan_repo()
            assert any("corpus.py" in v for v in violations), (
                f"injected violation was not caught: {violations}"
            )
        finally:
            target.write_text(original)
            _scan_repo.cache_clear()


# ── The actual gate ──────────────────────────────────────────────────────


class TestRepoWideScan:
    def test_no_live_log_call_passes_message_kwarg(self) -> None:
        violations = list(_scan_repo())
        assert violations == [], (
            f"found _log/log call(s) passing message= — a reserved "
            f"LogRecord attribute that raises KeyError under stdlib-"
            f"routed structlog (nexus-z0idx): {violations}"
        )
