# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs the REAL Ant logic of the jOOQ codegen skip (``service/pom.xml``, nexus-bnc56).

``tests/test_service_jooq_codegen_skip_lint.py`` reads the pom's XML shape. This file
EXECUTES the three antrun steps (``jooq-inputs`` at initialize, ``jooq-stamp-main`` at
process-sources, ``jooq-stamp-test`` at process-test-sources) under Apache Ant 1.10.12,
the version maven-antrun-plugin 3.1.0 declares, with a scratch project standing in for
``service/`` and a fake ``generate`` that writes a file recording which inputs it saw
(the pom, the changelogs, and since nexus-aca0q.3 the codegen runner source and the two
generator XML files).
The Maven phases are replayed by hand, so a build can stop at any phase or die in the
middle of a generation.

The invariant under test is the one the first review round broke: **a stamp exists only
while the generated tree matches it**. After EVERY step of EVERY scenario the harness
checks that a stamp on disk equals the hash the tree was generated from; and whenever
the pom's decision is "skip", the tree must match the current inputs. A regeneration
that never reaches its stamp phase (``generate-sources`` alone, a Ctrl-C, a failed
codegen, a ``-Dnexus.jooq.regenerate=true`` run killed mid-write) is exactly what used to
leave an old stamp over a new tree.

Ant is taken from ``~/.m2`` (any engine build puts it there) or downloaded once from
Maven Central, and verified against pinned SHA-256 digests either way. A machine with no
JVM, or with neither source of Ant, FAILS this file instead of skipping it: a skip would
turn the safety net into a pass over nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_POM = Path(__file__).resolve().parents[1] / "service" / "pom.xml"

#: nexus-aca0q.3: the generation inputs beside the changelogs (service/src/build/...), per kind.
_RUNNER = "src/build/java/JooqCodegen.java"
_RECIPE = "src/build/java/dev/nexus/build/PgContainerRecipe.java"
_GENERATOR = {"main": "src/build/jooq/jooq-main.xml", "test": "src/build/jooq/jooq-test.xml"}

_ANT_VERSION = "1.10.12"  # what maven-antrun-plugin 3.1.0 declares
_ANT_JARS = {
    "ant": "5c6a438c3ebe7a306eba452b09fa307b0e60314926177920bca896c4a504eaf6",
    "ant-launcher": "42a7ca7e7bb777fe7ee75a5ed4cc529c766bc43486367fdaad0ab4a32ee9c249",
}


# --------------------------------------------------------------------------- Ant


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: nexus-9st84: Maven Central answered a transient 404 for this pinned, existing artifact once in
#: thirty develop runs (CI 39720885850), and one bare urlopen errored the whole module at setup. The
#: download now tries each host up to three times with a short wait; the pinned sha256 still decides.
_CENTRAL_HOSTS = ("https://repo.maven.apache.org/maven2", "https://repo1.maven.org/maven2")
_FETCH_ATTEMPTS = 3
_FETCH_WAIT_S = 2.0
_urlopen = urllib.request.urlopen  # module-local seam: a test replaces this, never urllib's own


def _download(rel: str, dest: Path, *, wait=time.sleep) -> None:
    """Fetch *rel* from Maven Central into *dest* (via a per-process part file), retrying a
    transient failure. The caller checks the digest."""
    errors: list[str] = []
    for attempt in range(_FETCH_ATTEMPTS):
        for host in _CENTRAL_HOSTS:
            try:
                with _urlopen(f"{host}/{rel}", timeout=60) as r:
                    data = r.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{host} attempt {attempt + 1}: {exc}")
                continue
            part = dest.with_suffix(f".part.{os.getpid()}")
            part.write_bytes(data)
            part.replace(dest)
            return
        if attempt + 1 < _FETCH_ATTEMPTS:
            wait(_FETCH_WAIT_S * (attempt + 1))
    raise AssertionError(f"could not download {rel} from Maven Central: " + "; ".join(errors))


def _ant_jar(name: str) -> Path:
    jar = f"{name}-{_ANT_VERSION}.jar"
    rel = f"org/apache/ant/{name}/{_ANT_VERSION}/{jar}"
    local = Path.home() / ".m2" / "repository" / rel
    if local.is_file() and _sha256(local) == _ANT_JARS[name]:
        return local
    cached = Path(tempfile.gettempdir()) / f"nexus-ant-{_ANT_VERSION}" / jar
    if not (cached.is_file() and _sha256(cached) == _ANT_JARS[name]):
        cached.parent.mkdir(parents=True, exist_ok=True)
        _download(rel, cached)
    assert _sha256(cached) == _ANT_JARS[name], f"{jar}: digest differs from the pinned one"
    return cached


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data


def _scripted_urlopen(outcomes: list, seen: list[str]):
    """An urlopen stand-in: each call pops the next outcome, an exception to raise or bytes to return."""
    def fake(url: str, timeout: float) -> _FakeResponse:
        seen.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(outcome)
    return fake


def _http_404(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "Not Found", None, None)


def test_a_transient_central_failure_is_retried_and_the_jar_lands(tmp_path, monkeypatch) -> None:
    """nexus-9st84: a 404 from one host, then a refused connection from the other, then the bytes."""
    seen: list[str] = []
    waits: list[float] = []
    monkeypatch.setattr(
        sys.modules[__name__], "_urlopen",
        _scripted_urlopen([_http_404("a"), urllib.error.URLError("refused"), b"jar-bytes"], seen),
    )
    dest = tmp_path / "ant.jar"
    _download("org/apache/ant/ant/1.10.12/ant-1.10.12.jar", dest, wait=waits.append)
    assert dest.read_bytes() == b"jar-bytes"
    assert [u.split("/org/apache/")[0] for u in seen] == list(_CENTRAL_HOSTS) + [_CENTRAL_HOSTS[0]]
    assert waits == [_FETCH_WAIT_S]
    assert not list(tmp_path.glob("*.part.*")), "the part file is renamed into place, not left behind"


def test_a_download_that_never_succeeds_fails_loud_after_every_attempt(tmp_path, monkeypatch) -> None:
    seen: list[str] = []
    waits: list[float] = []
    calls = _FETCH_ATTEMPTS * len(_CENTRAL_HOSTS)
    monkeypatch.setattr(
        sys.modules[__name__], "_urlopen", _scripted_urlopen([_http_404("x")] * calls, seen),
    )
    with pytest.raises(AssertionError, match="could not download org/apache/ant"):
        _download("org/apache/ant/ant/1.10.12/ant-1.10.12.jar", tmp_path / "ant.jar", wait=waits.append)
    assert len(seen) == calls
    assert waits == [_FETCH_WAIT_S * n for n in range(1, _FETCH_ATTEMPTS)]
    assert not (tmp_path / "ant.jar").exists()


@pytest.fixture(scope="module")
def ant_classpath() -> str:
    assert shutil.which("java"), "no java on PATH: this file runs Ant and must not skip"
    return os.pathsep.join(str(_ant_jar(n)) for n in _ANT_JARS)


# ------------------------------------------------------------- pom extraction


def _pom_text() -> str:
    return _POM.read_text(encoding="utf-8")


def _execution(text: str, exec_id: str) -> tuple[str, str]:
    """(attributes of ``<target>``, inner Ant XML) of the base build's antrun execution."""
    for m in re.finditer(r"<execution>\s*<id>" + re.escape(exec_id) + r"</id>.*?</execution>", text, re.DOTALL):
        t = re.search(r"<target([^>]*)>(.*?)</target>", m.group(), re.DOTALL)
        if t:
            return t.group(1), t.group(2)
    raise AssertionError(f"no antrun execution {exec_id} with a <target> in service/pom.xml")


def _build_xml() -> str:
    text = _pom_text()
    steps = {n: _execution(text, i) for n, i in
             (("inputs", "jooq-inputs"), ("stamp-main", "jooq-stamp-main"), ("stamp-test", "jooq-stamp-test"))}
    ns = " ".join(sorted({a for attrs, _ in steps.values() for a in re.findall(r'xmlns:\w+="[^"]*"', attrs)}))
    body = "".join(f'<target name="{n}">{inner}</target>\n' for n, (_, inner) in steps.items())
    return (
        f'<project name="jooq-codegen-skip-harness" default="inputs" {ns}>\n{body}'
        '<target name="report"><echo file="${harness.out}/report.txt" '
        'message="main_skip=${nexus.jooq.main.skip}&#10;test_skip=${nexus.jooq.test.skip}&#10;'
        'main_hash=${nexus.jooq.main.hash}&#10;test_hash=${nexus.jooq.test.hash}&#10;'
        'main_restored=${nexus.jooq.main.restored}&#10;test_restored=${nexus.jooq.test.restored}&#10;'
        'cache_dir=${nexus.jooq.cache.dir}&#10;producer=${nexus.jooq.producer}&#10;'
        'main_torn=${nexus.jooq.main.entry.torn}&#10;test_torn=${nexus.jooq.test.entry.torn}"/></target>\n</project>\n'
    )


# --------------------------------------------------------------------- harness


@dataclass
class Decision:
    skip: dict[str, bool]
    hash: dict[str, str]
    #: kind -> True when the jooq-inputs step restored the tree from the shared cache
    restored: dict[str, bool]
    #: the cache directory the jooq-inputs step resolved (Maven exports it to the stamp steps)
    cache_dir: str = "off"
    #: what the jooq-inputs step says produced (and so may be served from) the cache entries
    producer: str = ""


@dataclass
class Project:
    root: Path
    ant_cp: str
    #: kind -> hash the generated tree was produced from (None = no tree / torn tree)
    tree_hash: dict[str, str | None] = field(default_factory=lambda: {"main": None, "test": None})
    generations: list[str] = field(default_factory=list)
    #: the shared jOOQ cache directory (nexus-fkhd3); "off" = no cache, as before; None = no -D at all,
    #: so the pom's own resolution (NX_JOOQ_CACHE, then the git common dir) is what runs
    cache: str | None = "off"
    env: dict[str, str] = field(default_factory=dict)
    #: stdout+stderr of every Ant run, in order (the cache's warnings are asserted on)
    log: list[str] = field(default_factory=list)

    # layout ---------------------------------------------------------------
    @property
    def target(self) -> Path:
        return self.root / "target"

    @property
    def gen_dir(self) -> dict[str, Path]:
        return {"main": self.target / "generated-sources" / "jooq",
                "test": self.target / "generated-test-sources" / "jooq"}

    def stamp(self, kind: str) -> Path:
        return self.target / "jooq-stamps" / f"{kind}.sha256"

    def put(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    # Ant ------------------------------------------------------------------
    def ant(self, *targets: str, props: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        defines = {
            "project.basedir": str(self.root),
            "project.build.directory": str(self.target),
            "jooq.generated.dir": str(self.gen_dir["main"]),
            "jooq.generated.test.dir": str(self.gen_dir["test"]),
            "jooq.version": "3.20.11",
            "postgresql.version": "42.7.4",
            "testcontainers.version": "1.21.4",
            "liquibase.version": "4.29.0",
            "harness.out": str(self.root / "out"),
            **({} if self.cache is None else {"nexus.jooq.cache.dir": self.cache}),
            **(props or {}),
        }
        (self.root / "out").mkdir(exist_ok=True)
        cmd = ["java", "-cp", self.ant_cp, "org.apache.tools.ant.launch.Launcher",
               "-q", "-f", str(self.root / "build.xml"), *[f"-D{k}={v}" for k, v in defines.items()], *targets]
        env = {k: v for k, v in os.environ.items() if k != "NX_JOOQ_CACHE"} | self.env
        # cwd is the scratch parent: a buggy relative cache path lands here, never in the repo
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False,
                              cwd=self.root.parent, env=env)
        self.log.append(done.stdout + done.stderr)
        return done

    def decide(self, props: dict[str, str] | None = None) -> Decision:
        done = self.ant("inputs", "report", props=props)
        assert done.returncode == 0, f"Ant failed:\n{done.stdout}\n{done.stderr}"
        kv = dict(line.split("=", 1) for line in (self.root / "out" / "report.txt").read_text().splitlines())
        d = Decision(skip={k: kv[f"{k}_skip"] == "true" for k in ("main", "test")},
                     hash={k: kv[f"{k}_hash"] for k in ("main", "test")},
                     restored={k: kv[f"{k}_restored"] == "true" for k in ("main", "test")},
                     cache_dir=kv["cache_dir"], producer=kv["producer"])
        for kind in ("main", "test"):
            if d.restored[kind]:  # the tree on disk is now the cache entry's: read what it really is
                self.tree_hash[kind] = self.disk_tree_hash(kind)
        return d

    def disk_tree_hash(self, kind: str) -> str | None:
        """The hash the generated tree on disk was produced from (None: no tree / torn tree)."""
        b = self.gen_dir[kind] / "B.java"
        return b.read_text(encoding="utf-8").split("// ")[1].split()[0] if b.is_file() else None

    # the fake codegen -------------------------------------------------------
    def inputs_digest(self, kind: str) -> str:
        files = sorted((self.root / "src" / "main" / "resources" / "db").rglob("*.xml"))
        if kind == "test":
            files += sorted((self.root / "src" / "test" / "resources" / "db").rglob("*.xml"))
        files += [self.root / _RUNNER, self.root / _GENERATOR[kind]]
        return hashlib.sha256(b"".join(f.read_bytes() for f in [self.root / "pom.xml", *files])).hexdigest()

    def generate(self, kind: str, d: Decision, *, killed: bool = False) -> None:
        """What the codegen plugin does when its ``skip`` is false; ``killed`` dies mid-write."""
        if d.skip[kind]:
            return
        self.generations.append(kind)
        shutil.rmtree(self.gen_dir[kind], ignore_errors=True)
        self.gen_dir[kind].mkdir(parents=True)
        self.tree_hash[kind] = None  # torn until the generation completes
        (self.gen_dir[kind] / "A.java").write_text("class A {}", encoding="utf-8")
        if killed:
            return
        # the content names the inputs AND the generation, so a restored/republished tree is identifiable
        (self.gen_dir[kind] / "B.java").write_text(
            f"class B {{}} // {d.hash[kind]} gen{len(self.generations)}", encoding="utf-8")
        self.tree_hash[kind] = d.hash[kind]

    # Maven phases, replayed ---------------------------------------------------
    def build(self, through: str = "process-test-sources", *, props: dict[str, str] | None = None,
              kill_in_generate: str | None = None) -> Decision:
        """Replay initialize .. ``through``; ``kill_in_generate`` dies in that kind's generation."""
        phases = ["initialize", "generate-sources", "process-sources",
                  "generate-test-sources", "process-test-sources"]
        d = self.decide(props)
        self.check_invariant()
        for kind in ("main", "test"):  # a skip is only honest while the tree matches the inputs
            if d.skip[kind]:
                assert self.tree_hash[kind] == d.hash[kind], (
                    f"{kind}: the pom decided to SKIP while the tree was generated from "
                    f"{self.tree_hash[kind]!r}, not the current inputs {d.hash[kind]!r}: a stale tree")
        plan = {"generate-sources": ("gen", "main"), "process-sources": ("stamp", "main"),
                "generate-test-sources": ("gen", "test"), "process-test-sources": ("stamp", "test")}
        for phase in phases[1:phases.index(through) + 1]:
            action, kind = plan[phase]
            if action == "gen":
                self.generate(kind, d, killed=(kill_in_generate == kind))
                if kill_in_generate == kind:
                    self.check_invariant()
                    return d
            else:
                # Maven carries the inputs step's exported properties (hash, skip) into the stamp step.
                done = self.ant(f"stamp-{kind}", props={**(props or {}), f"nexus.jooq.{kind}.hash": d.hash[kind],
                                                        f"nexus.jooq.{kind}.skip": str(d.skip[kind]).lower(),
                                                        "nexus.jooq.cache.dir": d.cache_dir,
                                                        "nexus.jooq.producer": d.producer})
                assert done.returncode == 0, f"stamp step failed:\n{done.stdout}\n{done.stderr}"
            self.check_invariant()
        return d

    def check_invariant(self) -> None:
        """A stamp on disk is the hash of the tree on disk, or it is not on disk."""
        for kind in ("main", "test"):
            stamp = self.stamp(kind)
            if stamp.is_file():
                assert self.tree_hash[kind] is not None and stamp.read_text().strip() == self.tree_hash[kind], (
                    f"{kind}: stamp {stamp.read_text().strip()!r} outlived the tree it vouched for "
                    f"(tree generated from {self.tree_hash[kind]!r})")


def _make_project(parent: Path, ant_cp: str, cache: str | None = "off") -> Project:
    """A scratch ``service/`` under *parent* holding the pom's real Ant steps and version-A inputs."""
    root = parent.resolve() / "service"
    p = Project(root=root, ant_cp=ant_cp, cache=cache)
    root.mkdir(parents=True)
    (root / "build.xml").write_text(_build_xml(), encoding="utf-8")
    p.put("pom.xml", "<project>A</project>")
    p.put("src/main/resources/db/changelog/master.xml", "<main>A</main>")
    p.put("src/main/resources/db/changelog/other.xml", "<main-other>A</main-other>")
    p.put("src/test/resources/db/changelog-test/test-master.xml", "<test>A</test>")
    p.put(_RUNNER, "class JooqCodegen {} // A")
    p.put(_RECIPE, "package dev.nexus.build; class PgContainerRecipe {} // A")
    p.put(_GENERATOR["main"], "<configuration>main A</configuration>")
    p.put(_GENERATOR["test"], "<configuration>test A</configuration>")
    # an unrelated file under resources/ that the hash must NOT cover
    p.put("src/main/resources/META-INF/nexus/release.properties", "stamp=1")
    return p


@pytest.fixture()
def proj(tmp_path: Path, ant_classpath: str) -> Project:
    return _make_project(tmp_path, ant_classpath)


# -------------------------------------------------------------------- scenarios


def test_cold_generates_both_and_stamps(proj: Project) -> None:
    d = proj.build()
    assert d.skip == {"main": False, "test": False}
    assert proj.generations == ["main", "test"]
    assert proj.stamp("main").is_file() and proj.stamp("test").is_file()


def test_warm_skips_both(proj: Project) -> None:
    proj.build()
    proj.generations.clear()
    d = proj.build()
    assert d.skip == {"main": True, "test": True}
    assert proj.generations == []


def test_a_main_changelog_edit_regenerates_both_because_the_test_run_applies_it(proj: Project) -> None:
    proj.build()
    proj.generations.clear()
    proj.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    d = proj.build()
    assert d.skip == {"main": False, "test": False}
    assert proj.generations == ["main", "test"]
    assert proj.build().skip == {"main": True, "test": True}


def test_a_test_changelog_edit_regenerates_only_the_test_tree(proj: Project) -> None:
    proj.build()
    proj.generations.clear()
    proj.put("src/test/resources/db/changelog-test/test-master.xml", "<test>B</test>")
    d = proj.build()
    assert d.skip == {"main": True, "test": False}
    assert proj.generations == ["test"]


def test_a_new_changelog_file_regenerates(proj: Project) -> None:
    proj.build()
    proj.put("src/main/resources/db/changelog/new.xml", "<main-new/>")
    assert proj.build().skip == {"main": False, "test": False}


def test_a_pom_edit_regenerates_both(proj: Project) -> None:
    proj.build()
    proj.put("pom.xml", "<project>B</project>")
    assert proj.build().skip == {"main": False, "test": False}


def test_a_runner_edit_regenerates_both(proj: Project) -> None:
    """The runner decides how the tree is produced (the container, the changelog apply, the jOOQ call)."""
    proj.build()
    proj.generations.clear()
    proj.put(_RUNNER, "class JooqCodegen {} // B")
    assert proj.build().skip == {"main": False, "test": False}
    assert proj.generations == ["main", "test"]


def test_a_container_recipe_edit_regenerates_both(proj: Project) -> None:
    """nexus-llba1: the runner compiles PgContainerRecipe with itself (image, tmpfs, flags), so an edit to it is a changed input."""
    proj.build()
    proj.generations.clear()
    proj.put(_RECIPE, "package dev.nexus.build; class PgContainerRecipe {} // B")
    d = proj.build()
    assert d.skip == {"main": False, "test": False}
    assert proj.generations == ["main", "test"]
    assert proj.build().skip == {"main": True, "test": True}


def test_a_generator_file_edit_regenerates_only_its_own_tree(proj: Project) -> None:
    """jooq-main.xml carries the includes, excludes, forced types and flags of the product tree, jooq-test.xml those of the test tree."""
    proj.build()
    proj.generations.clear()
    proj.put(_GENERATOR["main"], "<configuration>main B</configuration>")
    assert proj.build().skip == {"main": False, "test": True}
    assert proj.generations == ["main"]
    proj.generations.clear()
    proj.put(_GENERATOR["test"], "<configuration>test B</configuration>")
    assert proj.build().skip == {"main": True, "test": False}
    assert proj.generations == ["test"]


def test_files_outside_db_do_not_move_the_hash(proj: Project) -> None:
    proj.build()
    proj.put("src/main/resources/META-INF/nexus/release.properties", "stamp=2")
    assert proj.build().skip == {"main": True, "test": True}


def test_regenerate_true_regenerates_even_when_current(proj: Project) -> None:
    proj.build()
    proj.generations.clear()
    d = proj.build(props={"nexus.jooq.regenerate": "true"})
    assert d.skip == {"main": False, "test": False}
    assert proj.generations == ["main", "test"]
    assert proj.build().skip == {"main": True, "test": True}


@pytest.mark.parametrize("prop", ["jooq.version", "postgresql.version", "liquibase.version", "testcontainers.version"])
def test_a_dependency_version_override_regenerates_and_reverting_regenerates_again(proj: Project, prop: str) -> None:
    """A -D override changes the codegen classpath without changing a byte of the pom."""
    proj.build()
    assert proj.build(props={prop: "9.9.9"}).skip == {"main": False, "test": False}
    assert proj.build(props={prop: "9.9.9"}).skip == {"main": True, "test": True}
    assert proj.build().skip == {"main": False, "test": False}


def test_a_stamp_over_deleted_sources_regenerates(proj: Project) -> None:
    proj.build()
    shutil.rmtree(proj.gen_dir["main"])  # a partial clean: the stamp survives, the .java files do not
    assert proj.stamp("main").is_file()
    d = proj.decide()
    assert d.skip == {"main": False, "test": True}, "stamp matched but no generated .java exists: must regenerate"


def test_reverting_after_a_generate_sources_only_run_regenerates(proj: Project) -> None:
    """The first-round ship-blocker: A built, B generated but never stamped, back to A."""
    proj.build()
    proj.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    proj.build(through="generate-sources")  # main tree is now B's; no stamp phase reached
    proj.put("src/main/resources/db/changelog/other.xml", "<main-other>A</main-other>")
    proj.generations.clear()
    d = proj.build()  # the harness asserts a skip would be a stale-tree skip
    assert d.skip["main"] is False and "main" in proj.generations


def test_reverting_after_a_generate_test_sources_only_run_regenerates(proj: Project) -> None:
    proj.build()
    proj.put("src/test/resources/db/changelog-test/test-master.xml", "<test>B</test>")
    proj.build(through="generate-test-sources")  # test tree is B's; stamp-test never ran
    proj.put("src/test/resources/db/changelog-test/test-master.xml", "<test>A</test>")
    proj.generations.clear()
    d = proj.build()
    assert d.skip["test"] is False and "test" in proj.generations


@pytest.mark.parametrize("kind", ["main", "test"])
def test_reverting_after_a_codegen_killed_mid_write_regenerates(proj: Project, kind: str) -> None:
    proj.build()
    proj.put("src/test/resources/db/changelog-test/test-master.xml", "<test>B</test>")
    proj.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    proj.build(kill_in_generate=kind)
    proj.put("src/test/resources/db/changelog-test/test-master.xml", "<test>A</test>")
    proj.put("src/main/resources/db/changelog/other.xml", "<main-other>A</main-other>")
    proj.generations.clear()
    d = proj.build()
    assert d.skip[kind] is False and kind in proj.generations


@pytest.mark.parametrize("kind", ["main", "test"])
def test_a_regenerate_true_run_killed_mid_write_is_not_vouched_for_afterwards(proj: Project, kind: str) -> None:
    """Same inputs, torn tree: only the stamp's absence can tell the next build."""
    proj.build()
    proj.build(props={"nexus.jooq.regenerate": "true"}, kill_in_generate=kind)
    proj.generations.clear()
    d = proj.build()
    assert d.skip[kind] is False and kind in proj.generations


def test_a_compile_only_build_keeps_the_test_stamp_warm(proj: Project) -> None:
    """`mvn compile` stops before process-test-sources; it must not cost the next `mvn test` a regeneration."""
    proj.build()
    proj.generations.clear()
    proj.build(through="process-sources")
    d = proj.build()
    assert d.skip == {"main": True, "test": True}
    assert proj.generations == []


@pytest.mark.parametrize("prop", ["nexus.jooq.main.skip", "nexus.jooq.test.skip"])
def test_a_hand_set_skip_is_rejected_not_obeyed(proj: Project, prop: str) -> None:
    """Ant properties are immutable: a -D skip would silently beat the hash decision, and
    the stamp step would then vouch for a tree nobody generated."""
    proj.build()
    done = proj.ant("inputs", props={prop: "true"})
    assert done.returncode != 0, "a hand-set skip property must fail the build"
    assert "nexus.jooq.regenerate" in (done.stdout + done.stderr), "the failure must name the supported lever"


# ------------------------------------------------- the shared cache (nexus-fkhd3)
#
# A fresh worktree has an empty target/, so the stamp (which lives in target/) cannot vouch for
# anything and every worktree used to boot the codegen container and replay the whole changelog
# (~350 MB written). The generated trees are a pure function of the hash the stamp already
# compares, so they are published under <cache>/<kind>-<hash>/ after a successful generation and
# restored by the jooq-inputs step when the stamp does not match but the cache holds that hash.
# The stamp invariant is untouched: the restore deletes any stamp BEFORE it swaps the tree in, and
# only the stamp-* steps write one.


@pytest.fixture()
def cached(tmp_path: Path, ant_classpath: str) -> Project:
    """Worktree A of a pair sharing one cache (``other_worktree`` makes B)."""
    return _make_project(tmp_path / "wt-a", ant_classpath, cache=str(tmp_path / "jooq-cache"))


def _other_worktree(p: Project, name: str = "wt-b") -> Project:
    """A second, empty worktree with the same inputs and the same shared cache."""
    return _make_project(p.root.parent.parent / name, p.ant_cp, cache=p.cache)


#: what the pom's jooq-inputs step names as the producer unless a -D says otherwise
_PRODUCER = "tmpfs-codegen-runner"


def _entry(p: Project, kind: str, digest: str, producer: str = _PRODUCER) -> Path:
    """The cache entry for a hash AND a producer: the producer is part of the key."""
    assert p.cache and p.cache != "off"
    return Path(p.cache) / f"{kind}-{digest}-{producer}"


def _sources(root: Path) -> dict[str, str]:
    return {f.relative_to(root).as_posix(): f.read_text(encoding="utf-8") for f in sorted(root.rglob("*.java"))}


def test_a_generation_publishes_both_trees_to_the_shared_cache(cached: Project) -> None:
    d = cached.build()
    assert cached.generations == ["main", "test"]
    for kind in ("main", "test"):
        entry = _entry(cached, kind, d.hash[kind])
        assert (entry / "complete").is_file(), f"{kind}: the entry is published without its completeness marker"
        assert _sources(entry / "generated") == _sources(cached.gen_dir[kind]) != {}
        meta = (entry / "meta.properties").read_text(encoding="utf-8")
        assert f"hash={d.hash[kind]}" in meta and f"producer={_PRODUCER}" in meta, "the entry names its inputs and its producer"
        tree = re.search(r"^treehash=(\w+)$", meta, re.MULTILINE)
        assert tree and (entry / "complete").read_text(encoding="utf-8").strip() == tree.group(1), (
            "the marker carries the digest of the tree it closes, so a restore can tell a torn copy")
    assert [c.name for c in Path(cached.cache).iterdir() if c.name.startswith(".")] == [], "staging dirs left behind"


def test_a_fresh_worktree_restores_from_the_cache_and_never_runs_codegen(cached: Project) -> None:
    first = cached.build()
    fresh = _other_worktree(cached)
    assert not fresh.target.exists()
    for kind in ("main", "test"):  # age the entries: a restore must mark them recently used (the pruner keeps the newest)
        os.utime(_entry(cached, kind, first.hash[kind]), (1_000_000, 1_000_000))
    d = fresh.build()
    for kind in ("main", "test"):
        assert _entry(cached, kind, first.hash[kind]).stat().st_mtime > 1_000_000 + 86400
    assert d.skip == {"main": True, "test": True} and d.restored == {"main": True, "test": True}
    assert fresh.generations == [], "the codegen container would have booted"
    for kind in ("main", "test"):
        assert _sources(fresh.gen_dir[kind]) == _sources(cached.gen_dir[kind]) != {}
        assert fresh.stamp(kind).read_text().strip() == first.hash[kind], "stamped by the stamp step, from the cache's hash"
    again = fresh.build()  # and the next build is an ordinary stamp skip, not another restore
    assert again.skip == {"main": True, "test": True} and again.restored == {"main": False, "test": False}


def test_a_clean_in_the_same_worktree_restores_too(cached: Project) -> None:
    cached.build()
    shutil.rmtree(cached.target)  # mvn clean
    cached.tree_hash = {"main": None, "test": None}
    cached.generations.clear()
    d = cached.build()
    assert d.restored == {"main": True, "test": True} and cached.generations == []


def test_a_changelog_change_regenerates_and_publishes_under_a_new_key_and_the_old_key_still_restores(
        cached: Project) -> None:
    a = cached.build()
    cached.generations.clear()
    cached.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    b = cached.build()
    assert b.skip == {"main": False, "test": False} and b.restored == {"main": False, "test": False}
    assert cached.generations == ["main", "test"]
    for kind in ("main", "test"):
        assert a.hash[kind] != b.hash[kind]
        assert (_entry(cached, kind, a.hash[kind]) / "complete").is_file(), "the old key was lost"
        assert (_entry(cached, kind, b.hash[kind]) / "complete").is_file(), "the new key was not published"
    # a fresh worktree on the NEW inputs restores B's trees; on the OLD inputs it restores A's
    wt_b = _other_worktree(cached)
    wt_b.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    assert wt_b.build().restored == {"main": True, "test": True}
    assert wt_b.disk_tree_hash("main") == b.hash["main"]
    wt_c = _other_worktree(cached, "wt-c")
    assert wt_c.build().restored == {"main": True, "test": True}
    assert wt_c.disk_tree_hash("main") == a.hash["main"] and wt_c.generations == []


def test_restore_swaps_out_a_stale_tree_and_its_stamp_before_the_stamp_step_runs(cached: Project) -> None:
    """Built A, then B (tree and stamp are B's), then back to A: a stamp that outlived the swap would
    vouch for the wrong tree. The harness asserts the invariant after every phase; this pins the end."""
    a = cached.build()
    cached.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    cached.build()
    cached.put("src/main/resources/db/changelog/other.xml", "<main-other>A</main-other>")
    cached.generations.clear()
    d = cached.decide()  # the initialize phase alone: tree swapped, stale stamp already gone
    assert d.restored == {"main": True, "test": True} and d.hash == a.hash
    assert cached.disk_tree_hash("main") == a.hash["main"]
    assert not cached.stamp("main").exists() and not cached.stamp("test").exists()
    cached.check_invariant()
    assert cached.build().skip == {"main": True, "test": True} and cached.generations == []


@pytest.mark.parametrize("kind", ["main", "test"])
def test_restore_heals_a_torn_tree_left_by_a_killed_codegen(cached: Project, kind: str) -> None:
    a = cached.build()
    cached.put("src/test/resources/db/changelog-test/test-master.xml", "<test>B</test>")
    cached.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
    cached.build(kill_in_generate=kind)
    cached.put("src/test/resources/db/changelog-test/test-master.xml", "<test>A</test>")
    cached.put("src/main/resources/db/changelog/other.xml", "<main-other>A</main-other>")
    cached.generations.clear()
    d = cached.build()
    assert d.restored[kind] and kind not in cached.generations
    assert cached.disk_tree_hash(kind) == a.hash[kind] and (cached.gen_dir[kind] / "B.java").is_file()


def test_regenerate_true_bypasses_the_cache_and_republishes_the_fresh_tree(cached: Project) -> None:
    cached.build()
    cached.generations.clear()
    d = cached.build(props={"nexus.jooq.regenerate": "true"})
    assert d.restored == {"main": False, "test": False} and cached.generations == ["main", "test"]
    for kind in ("main", "test"):
        assert _sources(_entry(cached, kind, d.hash[kind]) / "generated") == _sources(cached.gen_dir[kind]), (
            "-Dnexus.jooq.regenerate=true is the answer to the floating image tag: it must replace the entry")


def test_an_entry_without_its_marker_or_sources_is_regenerated_and_repaired(cached: Project) -> None:
    a = cached.build()
    (_entry(cached, "main", a.hash["main"]) / "complete").unlink()
    shutil.rmtree(_entry(cached, "test", a.hash["test"]) / "generated")  # marker present, nothing to restore
    fresh = _other_worktree(cached)
    d = fresh.build()
    assert d.restored == {"main": False, "test": False} and fresh.generations == ["main", "test"]
    for kind in ("main", "test"):
        entry = _entry(cached, kind, a.hash[kind])
        assert (entry / "complete").is_file() and _sources(entry / "generated") == _sources(fresh.gen_dir[kind]) != {}


@pytest.mark.parametrize("kind", ["main", "test"])
def test_an_entry_whose_marker_is_empty_is_not_usable_and_is_repaired(cached: Project, kind: str) -> None:
    """An empty ``complete`` (a copy that created the file and died before writing the digest) is never
    restored (no digest to check the copy against). It must also not count as a usable entry when
    publishing: otherwise the stamp step leaves it alone and every later build regenerates (nexus-k6zxp)."""
    a = cached.build()
    marker = _entry(cached, kind, a.hash[kind]) / "complete"
    assert marker.stat().st_size > 0
    marker.write_text("", encoding="utf-8")
    fresh = _other_worktree(cached)
    d = fresh.build()
    assert d.restored[kind] is False and kind in fresh.generations, "an entry with an empty marker must not be restored"
    assert marker.read_text(encoding="utf-8").strip() != "", "the stamp step must repair an empty marker"
    healed = _other_worktree(cached, "wt-c")
    h = healed.build()
    assert h.restored[kind] is True and healed.generations == [], "after the repair the entry restores"
    assert _sources(_entry(cached, kind, a.hash[kind]) / "generated") == _sources(fresh.gen_dir[kind])


#: peers racing to publish one absent entry in the test below
_RACING_PEERS = 4

#: Per-peer JVM option variables whose "Picked up ..." banner lands on stderr of every JVM the peer starts,
#: the placement helper's included (GitHub's ubuntu images export JAVA_TOOL_OPTIONS). The pom must read the
#: helper's exit code, never its output: peer 0 sees the banner on one variable, peer 1 on the other, peer 2
#: on both, peer 3 on neither (nexus-mjudo review).
_BANNER_ENVS: tuple[dict[str, str], ...] = (
    {"JAVA_TOOL_OPTIONS": "-Dnx.probe=1"},
    {"JDK_JAVA_OPTIONS": "-Dnx.probe=1"},
    {"JAVA_TOOL_OPTIONS": "-Dnx.probe=1", "JDK_JAVA_OPTIONS": "-Dnx.probe=1"},
    {},
)


def test_publishing_never_exposes_a_partial_entry(cached: Project, tmp_path: Path) -> None:
    """Publication is a rename from a staging dir: a reader that sees <kind>-<hash> sees all of it."""
    a = cached.build()
    entry = _entry(cached, "main", a.hash["main"])
    want = _sources(entry / "generated")
    shutil.rmtree(entry)
    # Several worktrees with identical inputs and a populated tree publish at the same moment. "The same moment"
    # is made exact by a barrier in the harness's build.xml (never in the pom): each peer stages its whole copy,
    # marker last, then waits for `go`, so every peer has decided to publish and all of them reach the placement
    # together. Left to JVM start-up skew the peers mostly serialise, and the race was met only under machine load
    # (nexus-mjudo: red twice in full lint runs, green alone).
    go = tmp_path / "go"
    peers = []
    for i in range(_RACING_PEERS):
        peer = _other_worktree(cached, f"peer-{i}")
        shutil.copytree(cached.target, peer.target)
        build = peer.root / "build.xml"
        text = build.read_text(encoding="utf-8")
        staged = re.findall(r'<copy file="[^"]*/main\.cache-complete"[^>]*/>', text)
        assert len(staged) == 1, "the marker's staging copy was not found in stamp-main: the barrier would be vacuous"
        build.write_text(text.replace(staged[0], (
            f'{staged[0]}<touch file="{peer.root / "ready"}"/>'
            '<waitfor maxwait="120" maxwaitunit="second" checkevery="1" checkeveryunit="millisecond">'
            f'<available file="{go}"/></waitfor>')), encoding="utf-8")
        peers.append(peer)
    seen_partial: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            if entry.is_dir():
                try:
                    got = {"complete": (entry / "complete").is_file(),
                           "gen": _sources(entry / "generated") if (entry / "generated").is_dir() else None}
                except FileNotFoundError as e:  # the entry vanished mid-read: also not an atomic publish
                    got = {"vanished": str(e)}
                if not got.get("complete") or got.get("gen") != want:
                    seen_partial.append(repr(got))
            time.sleep(0.001)

    t = threading.Thread(target=reader)
    t.start()
    procs: list[subprocess.Popen[str]] = []
    try:
        procs = [subprocess.Popen(  # the stamp step of each peer: its stamp matches, the entry is absent
            ["java", "-cp", peer.ant_cp, "org.apache.tools.ant.launch.Launcher", "-f", str(peer.root / "build.xml"),
             f"-Dproject.basedir={peer.root}", f"-Dproject.build.directory={peer.target}",
             f"-Djooq.generated.dir={peer.gen_dir['main']}", f"-Djooq.generated.test.dir={peer.gen_dir['test']}",
             f"-Dnexus.jooq.cache.dir={peer.cache}", f"-Dnexus.jooq.main.hash={a.hash['main']}",
             f"-Dnexus.jooq.producer={_PRODUCER}",
             "-Dnexus.jooq.main.skip=true", "-Djooq.version=3.20.11", "stamp-main"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=peer.root.parent,
            env={k: v for k, v in os.environ.items() if k not in ("NX_JOOQ_CACHE", *_BANNER_ENVS[2])}
                | _BANNER_ENVS[i % len(_BANNER_ENVS)]) for i, peer in enumerate(peers)]
        deadline = time.monotonic() + 120
        while not all((peer.root / "ready").is_file() for peer in peers):
            assert time.monotonic() < deadline and all(pr.poll() is None for pr in procs), \
                "a peer never reached the barrier"
            time.sleep(0.01)
        go.touch()
        outs = [pr.communicate(timeout=120)[0] for pr in procs]
        assert [pr.returncode for pr in procs] == [0] * len(procs), outs
    finally:
        for pr in procs:  # a peer left waiting at the barrier must not outlive the test
            if pr.poll() is None:
                pr.kill()
                pr.wait()
        stop.set()
        t.join()
    assert seen_partial == [], f"a reader saw a half-published entry: {seen_partial[:3]}"
    # The placement is exclusive: every peer staged a whole copy before any placed one, so exactly one places
    # the entry and the others are refused and say so. A second publisher is what rewrote the live entry under
    # the reader: Ant's <move> merged into it file by file, or moved the usable entry aside for its own copy.
    placed = [o for o in outs if "jOOQ cache: published the main tree" in o]
    assert len(placed) == 1, f"{len(placed)} of {len(peers)} peers published the same absent entry:\n" + "\n".join(outs)
    assert sum("Picked up" in o for o in outs) >= 3, "the JVM option banner never reached a peer: the check is vacuous"
    assert sum("the main tree was not published" in o for o in outs) == len(peers) - 1, outs
    assert (entry / "complete").is_file() and _sources(entry / "generated") == want
    assert [c.name for c in Path(cached.cache).iterdir() if c.name.startswith(".")] == [], "staging dirs left behind"


def test_a_placement_helper_that_cannot_run_is_reported_as_that_not_as_a_usable_entry(cached: Project) -> None:
    """The rename runs as a source-launcher program, which needs jdk.compiler. A JVM without it (or a failed
    fork) exits non-zero with no verdict; the build must say the helper could not run, naming the cause class,
    and not claim "the entry is usable already" (nexus-mjudo review)."""
    a = cached.build()
    entry = _entry(cached, "main", a.hash["main"])
    shutil.rmtree(entry)
    peer = _other_worktree(cached, "peer-nohelper")
    shutil.copytree(cached.target, peer.target)
    build = peer.root / "build.xml"
    text = build.read_text(encoding="utf-8")
    args = re.findall(r'<arg file="\$\{nexus\.jooq\.main\.stage\}"/>', text)
    assert len(args) == 2, "the pom's two placement calls were not found in stamp-main: the forced failure would be vacuous"
    # a JVM that does not have the compiler module: the source launcher refuses to start
    build.write_text(text.replace(args[0], '<jvmarg value="--limit-modules=java.base"/>' + args[0]), encoding="utf-8")
    done = peer.ant("stamp-main", props={
        "nexus.jooq.main.hash": a.hash["main"], "nexus.jooq.producer": _PRODUCER,
        "nexus.jooq.main.skip": "true", "nexus.jooq.cache.dir": peer.cache})
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "placement helper could not run" in out, out
    assert "usable already" not in out, "a helper that could not run was reported as a refused placement:\n" + out
    assert not entry.exists(), "nothing could have placed the entry"
    assert [c.name for c in Path(peer.cache).iterdir() if c.name.startswith(".")] == [], "staging dirs left behind"


def test_cache_off_touches_no_directory(proj: Project) -> None:
    proj.build()
    assert not (proj.root.parent / "off").exists() and not (proj.root / "off").exists()


def test_the_cache_is_found_through_NX_JOOQ_CACHE_then_the_git_common_dir(tmp_path: Path, ant_classpath: str) -> None:
    env_cache = tmp_path / "env-cache"
    via_env = _make_project(tmp_path / "via-env", ant_classpath, cache=None)
    via_env.env = {"NX_JOOQ_CACHE": str(env_cache)}
    d = via_env.build()
    assert (env_cache / f"main-{d.hash['main']}-{_PRODUCER}" / "complete").is_file()

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    in_repo = _make_project(repo, ant_classpath, cache=None)
    in_repo.build()
    common = repo / ".git" / "nexus-jooq-cache"
    assert (common / f"main-{d.hash['main']}-{_PRODUCER}" / "complete").is_file(), "default location is the git common dir"

    # Not a repository and no NX_JOOQ_CACHE: no cache, and the build still works exactly as before.
    lone = _make_project(tmp_path / "lone", ant_classpath, cache=None)
    lone.env = {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    lone.build()
    assert lone.generations == ["main", "test"]
    assert sorted(c.name for c in (tmp_path / "lone").iterdir()) == ["service"]

    off = _make_project(tmp_path / "off-env", ant_classpath, cache=None)
    off.env = {"NX_JOOQ_CACHE": "off"}
    off.build()
    assert not (tmp_path / "off-env" / "off").exists()


# ----------------------------- the cache is best-effort, and a torn entry is never vouched for


def test_the_default_producer_is_the_one_these_tests_assume(cached: Project) -> None:
    assert cached.build().producer == _PRODUCER


def test_a_cache_root_that_cannot_be_created_never_fails_the_build(tmp_path: Path, ant_classpath: str) -> None:
    """A regular file where the cache directory should be: unwritable for every user, root included."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    p = _make_project(tmp_path / "wt", ant_classpath, cache=str(blocker / "cache"))
    d = p.build()  # every Ant step must exit 0
    assert p.generations == ["main", "test"] and d.skip == {"main": False, "test": False}
    assert p.stamp("main").is_file() and p.stamp("test").is_file(), "the build is stamped as without a cache"
    assert blocker.read_text(encoding="utf-8") == "not a directory"
    assert any("not published" in chunk for chunk in p.log), "the cache's failure must be said, not swallowed"
    assert p.build().skip == {"main": True, "test": True}, "and the next build is an ordinary stamp skip"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes; the regular-file variant above covers it")
def test_a_read_only_cache_dir_never_fails_the_build_and_still_serves_restores(tmp_path: Path, ant_classpath: str) -> None:
    cache = tmp_path / "jooq-cache"
    first = _make_project(tmp_path / "wt-a", ant_classpath, cache=str(cache))
    a = first.build()
    cache.chmod(0o555)
    try:
        assert not os.access(cache, os.W_OK), "the fixture must really be read-only"
        fresh = _make_project(tmp_path / "wt-b", ant_classpath, cache=str(cache))
        d = fresh.build()
        assert d.restored == {"main": True, "test": True} and fresh.generations == [], "reading needs no write access"
        # a different key cannot be published into it: the build still succeeds, generating
        other = _make_project(tmp_path / "wt-c", ant_classpath, cache=str(cache))
        other.put("src/main/resources/db/changelog/other.xml", "<main-other>B</main-other>")
        o = other.build()
        assert other.generations == ["main", "test"] and any("not published" in c for c in other.log)
        assert not _entry(other, "main", o.hash["main"]).exists()
        assert (_entry(first, "main", a.hash["main"]) / "complete").is_file()
    finally:
        cache.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through modes")
def test_an_unreadable_entry_falls_through_to_a_regeneration(tmp_path: Path, ant_classpath: str) -> None:
    cached = _make_project(tmp_path / "wt-a", ant_classpath, cache=str(tmp_path / "jooq-cache"))
    a = cached.build()
    victim = _entry(cached, "main", a.hash["main"])
    victim.chmod(0o000)
    try:
        fresh = _other_worktree(cached)
        d = fresh.build()
        assert d.restored["main"] is False and "main" in fresh.generations, "an unreadable entry must be regenerated over"
        assert d.restored["test"] is True, "the other kind's entry is untouched"
    finally:
        for leftover in Path(cached.cache).iterdir():
            leftover.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through modes")
def test_an_unreadable_generated_dir_in_an_entry_falls_through_to_a_regeneration(tmp_path: Path, ant_classpath: str) -> None:
    cached = _make_project(tmp_path / "wt-a", ant_classpath, cache=str(tmp_path / "jooq-cache"))
    a = cached.build()
    gen = _entry(cached, "main", a.hash["main"]) / "generated"
    gen.chmod(0o000)
    try:
        fresh = _other_worktree(cached)
        d = fresh.build()
        assert d.restored["main"] is False and "main" in fresh.generations
    finally:
        gen.chmod(0o755)
        for leftover in Path(cached.cache).rglob("*"):
            if leftover.is_dir():
                leftover.chmod(0o755)


@pytest.mark.parametrize("damage", ["missing-file", "truncated-file", "extra-file"])
@pytest.mark.parametrize("kind", ["main", "test"])
def test_a_torn_entry_is_neither_restored_nor_stamped_and_is_repaired(cached: Project, kind: str, damage: str) -> None:
    """The review's ship-blocker shape: the marker is there, a file is not (or is wrong)."""
    a = cached.build()
    gen = _entry(cached, kind, a.hash[kind]) / "generated"
    if damage == "missing-file":
        (gen / "B.java").unlink()  # marker present, >= 1 java file left: a count-only test passes this
    elif damage == "truncated-file":
        (gen / "B.java").write_text("class B {", encoding="utf-8")
    else:
        (gen / "Stray.java").write_text("class Stray {}", encoding="utf-8")
    fresh = _other_worktree(cached)
    d = fresh.decide()
    assert d.restored[kind] is False, f"{kind}: a {damage} entry was restored"
    assert d.skip[kind] is False, f"{kind}: the codegen must run"
    assert not fresh.gen_dir[kind].exists(), "the half-copied tree must not be left for the codegen to build over"
    assert not fresh.stamp(kind).exists(), "no stamp may exist over a tree nobody vouched for"
    fresh.check_invariant()
    regen = _other_worktree(cached, "wt-c")
    regen.build()
    assert kind in regen.generations
    healed = _other_worktree(cached, "wt-d")
    h = healed.build()
    assert h.restored[kind] is True and healed.generations == [], "the torn entry was repaired by the regeneration"
    assert _sources(_entry(cached, kind, a.hash[kind]) / "generated") == _sources(regen.gen_dir[kind])


def _entry_snapshot(entry: Path) -> dict[str, bytes]:
    return {f.relative_to(entry).as_posix(): f.read_bytes() for f in sorted(entry.rglob("*")) if f.is_file()}


def _unwritable_destinations(p: Project) -> dict[str, str]:
    """-D overrides that put both generated dirs under a regular file: a copy into them fails for THIS reader
    only (the shape of a full disk or a destination it cannot write), for root as well as for anyone else."""
    blocker = p.root / "blocker"
    blocker.write_text("a file where a directory would have to be", encoding="utf-8")
    return {"jooq.generated.dir": str(blocker / "main"), "jooq.generated.test.dir": str(blocker / "test")}


def test_a_restore_that_fails_on_the_readers_side_leaves_the_shared_entry_whole_for_the_next_reader(cached: Project) -> None:
    """nexus-wchbg: a failed restore used to delete the entry whatever the cause, emptying one that other
    readers were copying from. A reader whose own destination cannot take the copy must leave the entry alone."""
    a = cached.build()
    before = {k: _entry_snapshot(_entry(cached, k, a.hash[k])) for k in ("main", "test")}
    assert all(any(n.endswith(".java") for n in snap) and "complete" in snap for snap in before.values())
    reader = _other_worktree(cached, "wt-failing")
    d = reader.decide(props=_unwritable_destinations(reader))
    assert d.restored == {"main": False, "test": False}, "the copy into an unwritable destination cannot have restored"
    assert any("could not be restored whole" in line for line in reader.log), "the failed restore must say so"
    for kind in ("main", "test"):
        assert _entry_snapshot(_entry(cached, kind, a.hash[kind])) == before[kind], (
            f"{kind}: a failure that was the reader's own damaged the shared entry")
    leftovers = sorted(f.name for f in Path(cached.cache).iterdir() if f.name.startswith("."))
    assert leftovers == [], f"a reader-local failure left cache debris: {leftovers}"
    other = _other_worktree(cached, "wt-next")
    n = other.build()
    assert n.restored == {"main": True, "test": True} and other.generations == [], (
        "the next reader could not restore from the entry the failing reader left behind")


@pytest.mark.parametrize("kind", ["main", "test"])
def test_an_entry_proven_torn_by_its_own_digest_is_renamed_aside_and_deleted_not_emptied_in_place(
        cached: Project, kind: str) -> None:
    a = cached.build()
    entry = _entry(cached, kind, a.hash[kind])
    (entry / "generated" / "B.java").write_text("class B {", encoding="utf-8")  # marker present, tree wrong
    reader = _other_worktree(cached, "wt-judge")
    d = reader.decide()
    assert d.restored[kind] is False
    assert not entry.exists(), "an entry its own digest proves torn must be taken out of service"
    assert sorted(f.name for f in Path(cached.cache).iterdir() if f.name.startswith(".")) == [], (
        "the renamed-aside entry must be deleted, not left behind")


def _report(p: Project) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (p.root / "out" / "report.txt").read_text().splitlines())


def _with_step_before(p: Project, anchor: str, step: str) -> None:
    """Splice *step* into the scratch build.xml right before the FIRST occurrence of *anchor* (the main kind comes first): the way a
    peer's action between two steps of the pom's restore is replayed deterministically."""
    build = p.root / "build.xml"
    text = build.read_text(encoding="utf-8")
    assert text.count(anchor) in (1, 2), f"the anchor is not in the pom's inputs step: {anchor}"
    build.write_text(text.replace(anchor, step + anchor, 1), encoding="utf-8")


def _cache_debris(cached: Project) -> list[str]:
    return sorted(f.name for f in Path(cached.cache).iterdir() if f.name.startswith("."))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through modes")
def test_one_unreadable_file_in_an_entry_never_fails_the_build_and_the_entry_is_left_alone(cached: Project) -> None:
    """nexus-wchbg critique: the judge digests the shared entry, and Ant's <checksum> throws on a file the reader
    cannot open (BUILD FAILED, AccessDeniedException). The header's promise is that an unusable entry costs a
    regeneration, never the build."""
    a = cached.build()
    entry = _entry(cached, "main", a.hash["main"])
    before = _entry_snapshot(entry)
    victim = entry / "generated" / "B.java"
    victim.chmod(0o000)
    try:
        assert not os.access(victim, os.R_OK), "the fixture must really be unreadable"
        reader = _other_worktree(cached, "wt-blind")
        d = reader.build()  # every Ant step must exit 0
        assert d.restored == {"main": False, "test": True}, "the entry this reader cannot read whole cannot have restored"
        assert "main" in reader.generations, "the codegen must run over the failed restore"
        assert any("is left as it is" in line for line in reader.log), "the reader must say it left the entry alone"
        assert _report(reader)["main_torn"] == "false", "an entry this reader cannot read is not proven torn"
    finally:
        victim.chmod(0o644)
    assert _entry_snapshot(entry) == before, "an entry the reader could not judge was changed"
    assert _cache_debris(cached) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through modes")
def test_a_file_that_becomes_unreadable_just_before_the_digest_cannot_fail_the_build_either(cached: Project) -> None:
    """A file can go unreadable (or an entry vanish) after the judge decided to digest. The digest is guarded: a nested
    build that dies inside it leaves no verdict, which is "cannot judge", not a failed build and not a torn entry."""
    a = cached.build()
    entry = _entry(cached, "main", a.hash["main"])
    before = _entry_snapshot(entry)
    victim = entry / "generated" / "B.java"
    reader = _other_worktree(cached, "wt-late")
    _with_step_before(reader, "<subant antfile=", f'<exec executable="chmod"><arg value="000"/><arg file="{victim}"/></exec>')
    try:
        d = reader.decide(props=_unwritable_destinations(reader))  # asserts the Ant run exited 0
        assert not os.access(victim, os.R_OK), "the injected chmod never ran: the check is vacuous"
        assert d.restored["main"] is False
        assert _report(reader)["main_torn"] == "false"
        assert not (reader.target / "jooq-stamps" / "main-entry-digest.txt").exists(), "a digest that died left a verdict"
    finally:
        victim.chmod(0o644)
    assert _entry_snapshot(entry) == before
    assert _cache_debris(cached) == []


@pytest.mark.parametrize("how", ["removed-between-the-restore-and-the-judge", "never-published"])
def test_an_entry_that_is_not_there_when_it_is_judged_is_never_condemned_and_the_build_succeeds(
        cached: Project, how: str) -> None:
    a = cached.build()
    entry = _entry(cached, "main", a.hash["main"])
    reader = _other_worktree(cached, "wt-vanish")
    if how == "never-published":
        shutil.rmtree(entry)
    else:  # a prune (or a peer's repair) renames the entry away after this reader decided to try it
        _with_step_before(reader, '<condition property="nexus.jooq.main.entry.judge"',
                          f'<delete dir="{entry}" quiet="true"/>')
    d = reader.decide(props=_unwritable_destinations(reader))
    assert d.restored["main"] is False and d.skip["main"] is False, "the codegen must run"
    assert not entry.exists()
    assert _report(reader)["main_torn"] == "false", "an entry that is gone cannot be proven torn"
    assert _cache_debris(cached) == [], "nothing was renamed aside: there was nothing to condemn"


def test_the_marker_is_read_again_before_the_judge_so_a_peers_repair_is_not_condemned_against_a_stale_one(
        cached: Project) -> None:
    """The entry's marker is wrong when this reader starts (so its restore fails and the entry is judged), and a peer
    repairs it during this reader's copy. The judge must compare the entry with the marker as it is NOW: against the
    value read at the start of the step, a whole entry would be renamed aside under every other reader."""
    a = cached.build()
    entry = _entry(cached, "main", a.hash["main"])
    good = (entry / "complete").read_text(encoding="utf-8").strip()
    before = {n: b for n, b in _entry_snapshot(entry).items() if n != "complete"}
    (entry / "complete").write_text("stale-marker-of-a-torn-publication", encoding="utf-8")
    reader = _other_worktree(cached, "wt-repaired")
    _with_step_before(reader, '<loadfile property="nexus.jooq.main.entry.marker"',
                      f'<echo file="{entry}/complete" message="{good}"/>')
    d = reader.decide(props=_unwritable_destinations(reader))
    assert d.restored["main"] is False
    assert (entry / "complete").read_text(encoding="utf-8").strip() == good, "the injected repair never ran: vacuous"
    assert _report(reader)["main_torn"] == "false", "a whole entry was condemned against a marker that was already replaced"
    assert {n: b for n, b in _entry_snapshot(entry).items() if n != "complete"} == before
    assert _cache_debris(cached) == []


def test_a_different_producer_never_serves_or_replaces_another_producers_tree(cached: Project) -> None:
    a = cached.build()
    other = "nexus-v0ts5-migrated-template"
    v = _other_worktree(cached, "wt-v")
    d = v.build(props={"nexus.jooq.producer": other})
    assert d.producer == other
    assert d.restored == {"main": False, "test": False} and v.generations == ["main", "test"], (
        "an entry made by another producer was served")
    for kind in ("main", "test"):
        assert (_entry(cached, kind, a.hash[kind]) / "complete").is_file(), "the original producer's entry was lost"
        assert (_entry(cached, kind, a.hash[kind], other) / "complete").is_file(), "the new producer's entry was not published"
        assert f"producer={other}" in (_entry(cached, kind, a.hash[kind], other) / "meta.properties").read_text()
    back = _other_worktree(cached, "wt-back")
    assert back.build().restored == {"main": True, "test": True} and back.generations == []
    again = _other_worktree(cached, "wt-v2")
    assert again.build(props={"nexus.jooq.producer": other}).restored == {"main": True, "test": True}


# The five cases above that rely on file modes cannot run as root (root reads and writes through any mode),
# so they skip there; the regular-file cases cover the same publish and restore paths for every user. The budget
# below is the whole of what may skip, and the census proves each skip is conditioned on root and nothing else, so
# a non-root run (a laptop, chas as nxtest) executes all of them.
MODE_CASES_MAX_SKIP = 5
_MODE_CASES = (
    test_a_read_only_cache_dir_never_fails_the_build_and_still_serves_restores,
    test_an_unreadable_entry_falls_through_to_a_regeneration,
    test_an_unreadable_generated_dir_in_an_entry_falls_through_to_a_regeneration,
    test_one_unreadable_file_in_an_entry_never_fails_the_build_and_the_entry_is_left_alone,
    test_a_file_that_becomes_unreadable_just_before_the_digest_cannot_fail_the_build_either,
)


def test_the_mode_based_cases_skip_for_root_and_for_nobody_else() -> None:
    assert len(_MODE_CASES) == MODE_CASES_MAX_SKIP
    for case in _MODE_CASES:
        skips = [m for m in case.pytestmark if m.name == "skipif"]
        assert len(skips) == 1, f"{case.__name__}: exactly one skip condition, found {len(skips)}"
        assert bool(skips[0].args[0]) == (os.geteuid() == 0), f"{case.__name__}: skips for something other than root"
