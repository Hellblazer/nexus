# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the two cuts that keep the Service CI Java job's disk writes down (nexus-km7ma).

Measured on the lab pool (run 39341824860 at 54a035937) the Java job wrote about 444 MB in its
cgroup. Two of the avoidable parts are pinned here:

* the job fetched every engine-service tag (575, about 121 MB) for the ONE tag
  ``SchemaUpgradeRehearsalIntegrationTest`` reads. The checkout now fetches no tags and a
  named step fetches ``OLD_TAG`` alone, read from the test's own constant.
* Liquibase wrote 32 MB of JUL INFO and 10 MB of UI text to the console of the main Maven step,
  and the same text into about 40 MB of surefire XML. The test forks (surefire only) now run
  with a quiet JUL config and ``liquibase.command.showSummary=OFF``. The engine's own console is
  parsed by ``tests/daemon/test_readiness.py`` ("Running Changeset"), so none of this may reach
  production: the properties file is a TEST resource and nothing but the surefire block uses it.

The behaviour test runs the workflow step's real shell against a throwaway origin, so a rewrite
of the step that still "looks right" but fetches the wrong thing fails here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "service-ci.yml"
_POM = _ROOT / "service" / "pom.xml"
_REHEARSAL = Path("service/src/test/java/dev/nexus/service/SchemaUpgradeRehearsalIntegrationTest.java")
_JOB = "java-test-and-drift-guard"
_FETCH_STEP = "Fetch the one old engine tag the upgrade rehearsal reads"
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}
_JUL_FILE = "logging-quiet-liquibase.properties"
_OLD_TAG_RE = re.compile(r'String OLD_TAG = "(engine-service-v[0-9.]*)";')


def _steps() -> list[dict]:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"][_JOB]["steps"]


def _checkout_step(steps: list[dict]) -> dict:
    found = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(found) == 1, f"expected one checkout step in {_JOB}, found {len(found)}"
    return found[0]


# --- B: only the needed tag is fetched ---------------------------------------------------------


def test_the_java_job_fetches_no_tags_at_checkout() -> None:
    step = _checkout_step(_steps())
    assert step["with"].get("fetch-tags") is False, "fetch-tags must be false: 575 tags cost ~121 MB of disk for one tag"


def test_the_one_tag_comes_from_the_rehearsals_own_constant_and_is_fetched_by_name() -> None:
    steps = _steps()
    names = [s.get("name") for s in steps]
    assert names.count(_FETCH_STEP) == 1, f"the tag-fetch step must exist exactly once; steps: {names[:8]}"
    # It runs before anything that reads the tag (the mvn steps), so right after the checkout.
    assert names.index(_FETCH_STEP) < next(i for i, s in enumerate(steps) if "mvnw" in str(s.get("run", ""))), (
        "the tag fetch must precede the Maven steps"
    )
    run = steps[names.index(_FETCH_STEP)]["run"]
    assert str(_REHEARSAL) in run and "OLD_TAG" in run, "the tag must be derived from the rehearsal's OLD_TAG constant"
    assert "git fetch --no-tags --depth 1 origin" in run and "refs/tags/${tag}:refs/tags/${tag}" in run, run
    assert "exit 1" in run, "an empty or ambiguous derived tag must fail the step"


def test_the_rehearsal_declares_exactly_one_old_tag_and_nothing_else_in_service_reads_tags() -> None:
    src = (_ROOT / _REHEARSAL).read_text(encoding="utf-8")
    assert len(_OLD_TAG_RE.findall(src)) == 1, "exactly one OLD_TAG constant, or the workflow's sed picks several"
    readers = []
    examined = 0
    for p in sorted((_ROOT / "service" / "src").rglob("*.java")):
        examined += 1
        text = p.read_text(encoding="utf-8")
        if re.search(r'"git",\s*"(archive|describe|tag|show-ref|for-each-ref)"', text) or "refs/tags/" in text:
            readers.append(p.name)
    assert examined > 200, f"the sweep examined only {examined} Java files: the tree moved, fix this census"
    assert readers == [_REHEARSAL.name], f"another test reads git tags, so the one-tag fetch is not enough: {readers}"


def test_the_non_vacuity_assert_on_the_rehearsal_is_still_wired() -> None:
    runs = " ".join(str(s.get("run", "")) for s in _steps())
    assert "assert_rehearsal_ran.py" in runs, "the rehearsal's skip guard must stay in the Java job"


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
def test_the_fetch_step_really_fetches_only_the_old_tag(tmp_path: Path) -> None:
    """Runs the step's own script against a throwaway origin holding two tags: only OLD_TAG may arrive."""
    steps = _steps()
    script = next(s["run"] for s in steps if s.get("name") == _FETCH_STEP)
    tag = _OLD_TAG_RE.search((_ROOT / _REHEARSAL).read_text(encoding="utf-8")).group(1)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "f.txt").write_text("one\n")
    git(origin, "add", "f.txt")
    git(origin, "commit", "-q", "-m", "one")
    git(origin, "tag", tag)
    (origin / "f.txt").write_text("two\n")
    git(origin, "commit", "-q", "-am", "two")
    git(origin, "tag", "engine-service-v9.9.9")

    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", f"file://{origin}")
    dest = work / _REHEARSAL
    dest.parent.mkdir(parents=True)
    shutil.copy(_ROOT / _REHEARSAL, dest)

    subprocess.run(["bash", "-e", "-c", script], cwd=work, check=True, capture_output=True, env=env)
    assert git(work, "tag", "--list").split() == [tag], "exactly the OLD_TAG tag must be present, no others"

    # An unparsable constant fails the step instead of fetching "everything" or nothing.
    dest.write_text("class X {}\n")
    r = subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "could not derive exactly one OLD_TAG" in r.stderr


def test_the_fetch_retries_a_transient_failure_and_still_fails_loud_on_a_persistent_one(tmp_path: Path) -> None:
    script = next(s["run"] for s in _steps() if s.get("name") == _FETCH_STEP)
    assert "for attempt in 1 2 3" in script and "sleep" in script
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    }
    real = shutil.which("git")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "count"
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    # A wrapper whose first FAIL_FIRST fetches fail, then delegates; sleep is a no-op.
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = fetch ]; then n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"; '
        'if [ "$n" -le "${FAIL_FIRST:-0}" ]; then exit 1; fi; fi\n'
        f'exec "{real}" "$@"\n'
    )
    for f in ("sleep", "git"):
        (bin_dir / f).chmod(0o755)
    tag = _OLD_TAG_RE.search((_ROOT / _REHEARSAL).read_text(encoding="utf-8")).group(1)
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (
        ["init", "-q"],
        ["-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "--allow-empty", "-m", "x"],
        ["tag", tag],
    ):
        subprocess.run([real, "-C", str(origin), *args], check=True, capture_output=True, env=env)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run([real, "-C", str(work), "init", "-q"], check=True, capture_output=True, env=env)
    subprocess.run([real, "-C", str(work), "remote", "add", "origin", f"file://{origin}"], check=True, env=env)
    dest = work / _REHEARSAL
    dest.parent.mkdir(parents=True)
    shutil.copy(_ROOT / _REHEARSAL, dest)
    run_env = {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    ok = subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True, env={**run_env, "FAIL_FIRST": "2"})
    assert ok.returncode == 0, ok.stderr
    assert counter.read_text().strip() == "3"

    counter.unlink()
    bad = subprocess.run(["bash", "-e", "-c", script], cwd=work, capture_output=True, text=True, env={**run_env, "FAIL_FIRST": "99"})
    assert bad.returncode != 0 and "after 3 attempts" in bad.stderr
    assert counter.read_text().strip() == "3"


# --- A: Liquibase is quiet in the surefire forks only ------------------------------------------


def _surefire_config() -> ET.Element:
    root = ET.parse(_POM).getroot()
    found = [
        p for p in root.iterfind(".//m:build/m:plugins/m:plugin", _NS)
        if p.findtext("m:artifactId", namespaces=_NS) == "maven-surefire-plugin"
    ]
    assert len(found) == 1, "expected one surefire plugin in build/plugins"
    cfg = found[0].find("m:configuration", _NS)
    assert cfg is not None
    return cfg


def test_the_surefire_forks_run_with_a_quiet_liquibase_jul_config() -> None:
    arg_line = (_surefire_config().findtext("m:argLine", namespaces=_NS) or "").strip()
    m = re.search(r"-Djava\.util\.logging\.config\.file=\$\{project\.basedir\}/(\S+)", arg_line)
    assert m, f"surefire argLine must point JUL at the quiet config: {arg_line!r}"
    target = _ROOT / "service" / m.group(1)
    assert target.name == _JUL_FILE and target.is_file(), f"{target} must exist"
    assert "src/test/resources" in m.group(1), "the JUL file is a TEST resource, never shipped"
    props = dict(
        line.split("=", 1) for line in target.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert props.get("liquibase.level") == "WARNING", "Liquibase INFO must be off in the test forks, WARNING kept"
    assert "-Duser.timezone=UTC" in arg_line, "the existing UTC pin (nexus-rph82) must survive the argLine edit"


def test_the_surefire_forks_turn_the_liquibase_update_summary_off() -> None:
    props = _surefire_config().find("m:systemPropertyVariables", _NS)
    assert props is not None
    assert props.findtext("m:liquibase.command.showSummary", namespaces=_NS) == "OFF"


def test_quiet_liquibase_never_reaches_the_production_engine() -> None:
    """tests/daemon/test_readiness.py parses 'Running Changeset' from the engine console."""
    pom = _POM.read_text(encoding="utf-8")
    root = ET.fromstring(pom)  # comments are dropped, so prose naming a property never counts
    surefire = [
        p for p in root.iterfind(".//m:build/m:plugins/m:plugin", _NS)
        if p.findtext("m:artifactId", namespaces=_NS) == "maven-surefire-plugin"
    ]
    assert len(surefire) == 1
    inside = {id(e) for e in surefire[0].iter()}
    setters = [
        e for e in root.iter()
        if e.tag.endswith("}liquibase.command.showSummary")
        or (e.tag.endswith("}argLine") and "java.util.logging.config.file" in (e.text or ""))
    ]
    assert setters, "the quiet-Liquibase settings must exist in the surefire block"
    outside = [e.tag for e in setters if id(e) not in inside]
    assert outside == [], f"only the surefire configuration may set the quiet-Liquibase flags: {outside}"
    assert not (_ROOT / "service" / "src" / "main" / "resources" / _JUL_FILE).exists()
    main_hits = [
        p.name for p in (_ROOT / "service" / "src" / "main").rglob("*")
        if p.is_file() and p.suffix in {".java", ".xml", ".properties", ".json"}
        and "java.util.logging.config.file" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert main_hits == [], f"production code must not set the JUL config file: {main_hits}"
