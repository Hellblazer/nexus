# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-84a6 / nexus-3xi4x: source-level guard preventing the
``json.loads(<CliRunner Result>.output)`` anti-pattern from reappearing.

Click 8.2 changed semantics: ``result.output`` is now stdout + stderr
interleaved, not just stdout. JSON-parsing the merged stream fails
non-deterministically on Linux CI when a structlog/warn line lands
before the JSON payload -- surfaces as ``JSONDecodeError`` with either
``Extra data`` or ``Expecting value`` (empty if the log flushes after
stdout was captured).

The fix: parse ``result.stdout`` instead. This test file enforces the
rule across the whole test tree so the rot can't sneak back in via
copy-paste.

nexus-3xi4x: the original guard was a literal regex anchored to the
variable name ``result`` (``json\\.loads\\(\\s*result\\.output\\s*\\)``).
This repo's own convention names ``CliRunner`` results descriptively
(``res``, ``js``, ``search``, ``wombat_search``, ...), so the regex
never fired on the exact class of bug it existed to catch -- proven by
two live instances on develop (test_census.py, test_catalog_cli.py) and
by the 2026-08-05 CI red in test_scenario_journeys.py that this guard
was supposed to prevent.

The guard is now AST-based and receiver-name-agnostic: it flags
``json.loads(X)`` wherever ``X`` is, or resolves through a chain of
attribute access / method calls / a *single*-assignment local variable,
to an expression that accesses a ``.output`` attribute. See
``_flows_from_output`` for the exact resolution rules and their honest
limits (ambiguous / multiply-reassigned locals are NOT resolved --
false negative by design, not a bug). A third inherited limit: the scan
globs ``test_*.py`` only, so a ``.output``-parsing helper living in
``conftest.py`` would be invisible (no live occurrence as of 2026-08-05;
extend the glob if one ever appears).
"""
from __future__ import annotations

import ast
from pathlib import Path

_AMBIGUOUS = object()  # sentinel: name assigned more than once in its scope


class _ScopeCollector(ast.NodeVisitor):
    """Collects single-assignment locals and ``json.loads(...)`` call
    sites within one lexical scope (module / function / class body),
    without descending into nested function/class/lambda scopes --
    those are recorded in ``subscopes`` for the caller to process with
    their own fresh collector, so names don't leak across scopes.
    """

    def __init__(self) -> None:
        self.assigns: dict[str, ast.expr | object] = {}
        self.calls: list[ast.Call] = []
        self.subscopes: list[ast.AST] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.subscopes.append(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.subscopes.append(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.subscopes.append(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        self.subscopes.append(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            self.assigns[name] = _AMBIGUOUS if name in self.assigns else node.value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "loads"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        ):
            self.calls.append(node)
        self.generic_visit(node)


def _flows_from_output(
    node: ast.expr,
    local_map: dict[str, ast.expr | object],
    _seen: set[str] | None = None,
) -> bool:
    """True if ``node`` is, or resolves through a chain of attribute
    access / method calls / single-assignment locals, to something
    that accesses a ``.output`` attribute.

    Handles (bead-enumerated cases):
      - direct: ``result.output``
      - chained/method calls on the receiver: ``result.output.strip()``
      - a single-assignment intermediate local: ``out.output`` then
        ``json.loads(out)``, including multi-hop chains of such locals.

    Honest limit: resolution stops -- and the call site is silently
    NOT flagged -- at a name that is assigned more than once in its
    enclosing scope (ambiguous which value flows to the call site), or
    is not a locally-tracked simple assignment at all (a function
    parameter, comprehension variable, import, or attribute/subscript
    target). Also not handled: ``.output`` passed as an argument to an
    unrelated wrapper call, e.g. ``str(result.output)`` -- only the
    direct-receiver and method-chain shapes above are covered. These
    are deliberate false negatives, not bugs; widen here if a new live
    instance proves the gap matters in practice.
    """
    seen = _seen if _seen is not None else set()
    if isinstance(node, ast.Attribute):
        if node.attr == "output":
            return True
        return _flows_from_output(node.value, local_map, seen)
    if isinstance(node, ast.Call):
        return _flows_from_output(node.func, local_map, seen)
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False
        value = local_map.get(node.id)
        if value is None or value is _AMBIGUOUS:
            return False
        seen.add(node.id)
        return _flows_from_output(value, local_map, seen)
    return False


def _scan(node: ast.AST, offenders_out: list[ast.Call]) -> None:
    """Recursively scan ``node`` (a Module, FunctionDef,
    AsyncFunctionDef, ClassDef, or Lambda) for offending
    ``json.loads(...)`` call sites, descending into nested scopes with
    fresh, scope-local assignment tracking.
    """
    if isinstance(node, ast.Lambda):
        # A lambda body is a single expression, not a statement list:
        # no local-assignment resolution is possible inside it, only
        # direct attribute/chain detection.
        for sub in ast.walk(node.body):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "loads"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "json"
                and sub.args
                and _flows_from_output(sub.args[0], {})
            ):
                offenders_out.append(sub)
        return

    body = getattr(node, "body", [])
    stmts = body if isinstance(body, list) else [body]

    collector = _ScopeCollector()
    for stmt in stmts:
        if stmt is not None:
            collector.visit(stmt)

    for call in collector.calls:
        if call.args and _flows_from_output(call.args[0], collector.assigns):
            offenders_out.append(call)

    for sub in collector.subscopes:
        _scan(sub, offenders_out)


def _find_offenders(scan_dir: Path, self_file: Path) -> list[tuple[Path, int, str]]:
    """Scan every ``test_*.py`` file under ``scan_dir`` (skipping
    ``self_file`` by basename, since this file legitimately discusses
    the anti-pattern) for offending ``json.loads(...)`` call sites.
    """
    offenders: list[tuple[Path, int, str]] = []
    for path in sorted(scan_dir.rglob("test_*.py")):
        if path.name == self_file.name:
            continue
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        calls: list[ast.Call] = []
        _scan(tree, calls)
        if not calls:
            continue
        lines = source.splitlines()
        for call in calls:
            lineno = call.lineno
            line_text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            offenders.append((path.relative_to(scan_dir), lineno, line_text))
    return offenders


def test_no_json_loads_on_result_output() -> None:
    """Every CliRunner-based test that JSON-parses the result must use
    ``result.stdout`` (clean), not ``result.output`` (stdout + stderr
    interleaved) -- under ANY receiver name. See nexus-84a6, nexus-3xi4x,
    and the Click 8.2 release notes.
    """
    tests_dir = Path(__file__).parent
    offenders = _find_offenders(tests_dir, Path(__file__))

    if offenders:
        sample = "\n  ".join(f"{p}:{n}  {line!r}" for p, n, line in offenders[:10])
        suffix = f"\n  ... (+{len(offenders) - 10} more)" if len(offenders) > 10 else ""
        raise AssertionError(
            "nexus-84a6/nexus-3xi4x: tests use json.loads(<expr>.output) which "
            "parses stdout+stderr interleaved (Click 8.2+ change). "
            "Switch to json.loads(<expr>.stdout):\n  " + sample + suffix
        )


def test_tripwire_fires_on_receiver_name_variants(tmp_path: Path) -> None:
    """Non-vacuity / kill-control proof: the detector actually fires,
    and does so regardless of the CliRunner result's variable name --
    the exact gap (literal ``result``-only regex) that let two live
    instances and the 2026-08-05 CI incident slip past the old guard.
    """
    fixture = tmp_path / "test_fixture_offenders.py"
    fixture.write_text(
        "import json\n"
        "\n"
        "def test_literal_result_name():\n"                 # line 3
        "    result = invoke()\n"                            # 4
        "    payload = json.loads(result.output)\n"          # 5  <- direct, literal name
        "\n"
        "def test_other_receiver_name():\n"                  # 7
        "    res = invoke()\n"                                # 8
        "    payload = json.loads(res.output)\n"             # 9  <- direct, non-'result' name
        "\n"
        "def test_wombat_receiver_name():\n"                 # 11
        "    js = invoke()\n"                                 # 12
        "    assert 'x' not in json.loads(js.output)\n"      # 13 <- direct, embedded in expr
        "\n"
        "def test_intermediate_variable():\n"                # 15
        "    wombat_search = invoke()\n"                      # 16
        "    raw = wombat_search.output\n"                    # 17
        "    payload = json.loads(raw)\n"                     # 18 <- single-assignment local
        "\n"
        "def test_strip_chain():\n"                           # 20
        "    octopus_search = invoke()\n"                      # 21
        "    payload = json.loads(octopus_search.output.strip())\n"  # 22 <- method chain
        "\n"
        "def test_clean_stdout_not_flagged():\n"              # 24
        "    result = invoke()\n"                              # 25
        "    payload = json.loads(result.stdout)\n"           # 26 <- must NOT be flagged
    )

    offenders = _find_offenders(tmp_path, Path(__file__))
    flagged_lines = {lineno for _, lineno, _ in offenders}

    assert flagged_lines == {5, 9, 13, 18, 22}, offenders


def test_tripwire_honest_limit_ambiguous_reassignment_not_flagged(tmp_path: Path) -> None:
    """Documents the detector's stated honest limit as an executable
    regression test: a local name reassigned more than once inside a
    scope is not resolved through -- the detector conservatively skips
    it rather than guessing which assignment flows to the call site.
    This is an intentional false negative, not a bug; see
    ``_flows_from_output``'s docstring. If this test starts failing
    because someone taught the resolver to handle reassignment, that's
    fine -- update the assertion, don't just delete the test.
    """
    fixture = tmp_path / "test_fixture_ambiguous.py"
    fixture.write_text(
        "import json\n"
        "\n"
        "def test_reassigned_local():\n"
        "    out = invoke()\n"
        "    out = out.output\n"  # reassignment -> ambiguous, not resolved
        "    payload = json.loads(out)\n"
    )

    offenders = _find_offenders(tmp_path, Path(__file__))
    assert offenders == []


def test_tripwire_does_not_flag_unrelated_json_loads(tmp_path: Path) -> None:
    """Negative control: ordinary json.loads() usage over files, HTTP
    bodies, and other non-CliRunner-``.output`` sources must not be
    flagged -- the detector's precision, not just its recall.
    """
    fixture = tmp_path / "test_fixture_clean.py"
    fixture.write_text(
        "import json\n"
        "\n"
        "def test_reads_file():\n"
        "    data = json.loads(path.read_text())\n"
        "\n"
        "def test_reads_stdout():\n"
        "    result = invoke()\n"
        "    payload = json.loads(result.stdout)\n"
        "\n"
        "def test_reads_response_body():\n"
        "    body = json.loads(request.content)\n"
    )

    offenders = _find_offenders(tmp_path, Path(__file__))
    assert offenders == []
