#!/usr/bin/env python3
"""Fail when a Java @Tag("integration") class did not actually execute tests.

nexus-yuogq. ``service/pom.xml`` sets ``test.excluded.groups=integration``, so
that group is excluded from the ordinary ``mvn test``. Until 2026-09-12 it ran
in no default suite, no release battery and no schedule, and three fixtures
rotted there undetected: two carried the RDR-204 unregistered-collection defect
and were found only because a bead named them by hand, and a third still errored
at SETUP afterwards with 12 tests dark. They are the SQL-function-versus-app-
stitch parity tests -- the cross-check between the combined-query functions and
the repository path, which is precisely the surface an engine release changes.

THE FAILURE THIS GUARDS IS A GREEN RUN THAT EXECUTED NOTHING, not a red test.
A class erroring at setup reports zero tests; a selector that stops matching (a
dropped tag, a renamed group, a filter change) exits 0 having run nothing. Both
read as success.

It deliberately does NOT assert a total count. The integration run writes into
the same surefire directory as the ordinary suite, so a total would be
satisfied by the ~2950 tests the ordinary suite already wrote and would pass
while the integration group ran zero -- vacuous in exactly the way this bead is
about. Instead it derives the expected class list from the SOURCE (every class
carrying the tag) and requires each one to have a report with tests > 0. That
is self-maintaining: adding or removing a tagged class needs no edit here.

SCOPE, stated so the guard is not trusted past it. It catches the case where the
group is SELECTED BUT EMPTY -- maven exits 0 having run nothing, because a tag
was dropped or a filter stopped matching -- which is silent. It does NOT
independently catch a class ERRORING at setup: surefire records that as one test
with an error, so this guard would pass it. That case is caught by the maven
step itself exiting non-zero, before this ever runs. It also assumes a CLEAN
target/: on a developer box carrying stale reports from an earlier run, a report
can satisfy this guard without the current run having produced it. CI checks out
fresh, and the ordinary suite writes no reports for these classes because they
are excluded from it, so both assumptions hold there.

nexus-gx2l1 tightened it in two directions.

EXECUTED, NOT MERELY REPORTED. A class whose tests were all SKIPPED
(``@EnabledIfEnvironmentVariable`` with no key, a failed ``Assumptions``, a
system-property gate) writes a report with ``tests=N skipped=N``; the first
version counted that as "ok N test(s)" and said so for ``EmbedParityTest`` with
no Voyage key. A class now counts only through its testcases that were not
skipped. A class whose tests all skipped FAILS unless it is on ``ALLOWED_SKIPS``,
where every entry carries the reason it skips by design in ``service-ci``. The
allowlist is closed in both directions: an entry whose class is no longer
tagged fails (a stale exemption is how the next skip hides), and an allowlisted
class that DOES execute tests is reported as such rather than silently passed.

REAL ANNOTATIONS, NOT TEXT. The expected class list used to be every file
containing the text ``@Tag("integration")``, so a class that quotes the tag in
javadoc (``RdrO8dil7GlobalManifestAntiJoinTest``) was expected to have run
under a group it is not in. Source is now passed through a comment-and-string
stripper (no Java parser) before the annotation is matched, so the text inside a
line comment, a block comment, javadoc, a string literal or a text block never
counts. Method-level tags count (``EmbedParityTest`` tags three methods).

Usage:  assert_integration_group_ran.py <service-dir>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET

# Classes that skip by design in service-ci, with the reason. Each reason is
# printed when the class is seen skipping, so the exemption is visible in the CI
# log every run rather than only in this file.
ALLOWED_SKIPS: dict[str, str] = {
    "dev.nexus.service.EmbedParityTest": (
        'its three @Tag("integration") tests call the live Voyage API and carry '
        '@EnabledIfEnvironmentVariable(VOYAGE_API_KEY); service-ci has no Voyage '
        "key, so they skip by design (nexus-gx2l1)"
    ),
    "dev.nexus.service.vectors.ChunkLiveOwnersRecallExtendedIntegrationTest": (
        "a manual measurement run gated by @EnabledIfSystemProperty "
        "nx.recallExt.run=true; service-ci never sets it (nexus-gx2l1)"
    ),
}

# @Tag("integration") as written in CODE: optional package qualifier, optional
# ``value =``. The literal is matched as a placeholder produced by strip_java(),
# never as raw text, so this regex cannot see a string or comment.
_TAG_RE = re.compile(
    r'@\s*(?:[\w.]*\.)?Tag\s*\(\s*(?:value\s*=\s*)?"<S(\d+)>"\s*\)'
)


def strip_java(src: str) -> tuple[str, list[str]]:
    """Return (code, literals): *src* with comments blanked and every string
    literal, text block and char literal replaced by ``"<S{i}>"`` (``literals[i]``
    holds its content). No Java parser: one forward scan with the five lexical
    states that can hide an annotation-shaped text."""
    out: list[str] = []
    lits: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        two = src[i : i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j  # keep the newline
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
        elif src.startswith('"""', i):
            j = i + 3
            while j < n and not (src.startswith('"""', j) and src[j - 1] != "\\"):
                j += 1
            lits.append(src[i + 3 : j])
            out.append(f'"<S{len(lits) - 1}>"' + "\n" * src.count("\n", i, j))
            i = min(n, j + 3)
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            lits.append(src[i + 1 : j])
            out.append(f'"<S{len(lits) - 1}>"')
            i = min(n, j + 1)
        else:
            out.append(c)
            i += 1
    return "".join(out), lits


def is_integration_tagged(src: str) -> bool:
    """True when *src* carries a real @Tag("integration") annotation."""
    code, lits = strip_java(src)
    return any(lits[int(m.group(1))] == "integration" for m in _TAG_RE.finditer(code))


def tagged_classes(test_root: str) -> list[str]:
    """Fully-qualified class names carrying a real @Tag("integration") annotation."""
    found = []
    for dirpath, _dirs, files in os.walk(test_root):
        for name in files:
            if not name.endswith(".java"):
                continue
            path = os.path.join(dirpath, name)
            try:
                src = open(path, encoding="utf-8").read()
            except OSError:
                continue
            if not is_integration_tagged(src):
                continue
            code, _lits = strip_java(src)
            m = re.search(r"^\s*package\s+([\w.]+)\s*;", code, re.M)
            if not m:
                continue
            found.append(f"{m.group(1)}.{name[:-5]}")
    return sorted(found)


def tests_in_report(reports_dir: str, fqcn: str) -> tuple[int, int] | None:
    """(executed, skipped) testcases for *fqcn*, or None when no report exists.

    Executed means a testcase without a ``<skipped>`` child. When the report
    lists no testcases at all, fall back to the suite attributes."""
    path = os.path.join(reports_dir, f"TEST-{fqcn}.xml")
    if not os.path.isfile(path):
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return (0, 0)
    cases = list(root.iter("testcase"))
    if cases:
        skipped = sum(1 for c in cases if c.find("skipped") is not None)
        return (len(cases) - skipped, skipped)
    tests = int(root.get("tests") or 0)
    skipped = min(int(root.get("skipped") or 0), tests)
    return (tests - skipped, skipped)


def main(argv: list[str] | None = None, allowed: dict[str, str] | None = None) -> int:
    allowed = ALLOWED_SKIPS if allowed is None else allowed
    ap = argparse.ArgumentParser()
    ap.add_argument("service_dir", help="the service/ directory")
    args = ap.parse_args(argv)

    test_root = os.path.join(args.service_dir, "src", "test", "java")
    reports = os.path.join(args.service_dir, "target", "surefire-reports")

    classes = tagged_classes(test_root)
    if not classes:
        print(
            'FAIL: no class carries a real @Tag("integration") annotation. Either the '
            "tag was renamed or the group was retired -- in both cases this guard and "
            "the CI step that runs the group are now theatre and must be updated or "
            "deleted (nexus-yuogq).",
            file=sys.stderr,
        )
        return 1

    bad: list[str] = []
    for fqcn in sorted(set(allowed) - set(classes)):
        bad.append(
            f"{fqcn}: STALE ALLOWLIST ENTRY (the class no longer carries a real "
            f"@Tag(\"integration\"); remove it from ALLOWED_SKIPS)"
        )
    for fqcn in classes:
        counts = tests_in_report(reports, fqcn)
        executed, skipped = counts if counts is not None else (0, 0)
        if fqcn in allowed and executed == 0:
            seen = "no report" if counts is None else f"{skipped} skipped"
            print(f"allowed-skip {fqcn}: {seen}; {allowed[fqcn]}")
        elif counts is None:
            bad.append(f"{fqcn}: NO REPORT (never selected, or the run never reached it)")
        elif executed == 0 and skipped:
            bad.append(
                f"{fqcn}: all {skipped} test(s) SKIPPED, none executed (a missing key, "
                "model or property, or a failed assumption; add it to ALLOWED_SKIPS with "
                "a reason only if it skips by design in service-ci)"
            )
        elif executed == 0:
            bad.append(f"{fqcn}: 0 tests (errored at SETUP, or every test was filtered out)")
        else:
            note = f", {skipped} skipped" if skipped else ""
            print(f"ok {fqcn}: {executed} test(s) executed{note}")

    if bad:
        print(
            f"\nFAIL: {len(bad)} problem(s) across {len(classes)} integration-tagged "
            "class(es). This is the nexus-yuogq failure -- a suite that executed "
            "nothing while exiting green, not a test that failed:",
            file=sys.stderr,
        )
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"\nall {len(classes)} integration-tagged class(es) executed tests or are allowlisted skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
