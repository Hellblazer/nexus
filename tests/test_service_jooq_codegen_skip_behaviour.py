# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs the REAL Ant logic of the jOOQ codegen skip (``service/pom.xml``, nexus-bnc56).

``tests/test_service_jooq_codegen_skip_lint.py`` reads the pom's XML shape. This file
EXECUTES the three antrun steps (``jooq-inputs`` at initialize, ``jooq-stamp-main`` at
process-sources, ``jooq-stamp-test`` at process-test-sources) under Apache Ant 1.10.12,
the version maven-antrun-plugin 3.1.0 declares, with a scratch project standing in for
``service/`` and a fake ``generate`` that writes a file recording which inputs it saw.
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
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_POM = Path(__file__).resolve().parents[1] / "service" / "pom.xml"

_ANT_VERSION = "1.10.12"  # what maven-antrun-plugin 3.1.0 declares
_ANT_JARS = {
    "ant": "5c6a438c3ebe7a306eba452b09fa307b0e60314926177920bca896c4a504eaf6",
    "ant-launcher": "42a7ca7e7bb777fe7ee75a5ed4cc529c766bc43486367fdaad0ab4a32ee9c249",
}


# --------------------------------------------------------------------------- Ant


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ant_jar(name: str) -> Path:
    jar = f"{name}-{_ANT_VERSION}.jar"
    rel = f"org/apache/ant/{name}/{_ANT_VERSION}/{jar}"
    local = Path.home() / ".m2" / "repository" / rel
    if local.is_file() and _sha256(local) == _ANT_JARS[name]:
        return local
    cached = Path(tempfile.gettempdir()) / f"nexus-ant-{_ANT_VERSION}" / jar
    if not (cached.is_file() and _sha256(cached) == _ANT_JARS[name]):
        cached.parent.mkdir(parents=True, exist_ok=True)
        part = cached.with_suffix(".part")
        with urllib.request.urlopen(f"https://repo.maven.apache.org/maven2/{rel}", timeout=60) as r:
            part.write_bytes(r.read())
        part.replace(cached)
    assert _sha256(cached) == _ANT_JARS[name], f"{jar}: digest differs from the pinned one"
    return cached


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
        'main_hash=${nexus.jooq.main.hash}&#10;test_hash=${nexus.jooq.test.hash}"/></target>\n</project>\n'
    )


# --------------------------------------------------------------------- harness


@dataclass
class Decision:
    skip: dict[str, bool]
    hash: dict[str, str]


@dataclass
class Project:
    root: Path
    ant_cp: str
    #: kind -> hash the generated tree was produced from (None = no tree / torn tree)
    tree_hash: dict[str, str | None] = field(default_factory=lambda: {"main": None, "test": None})
    generations: list[str] = field(default_factory=list)

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
            "harness.out": str(self.root / "out"),
            **(props or {}),
        }
        (self.root / "out").mkdir(exist_ok=True)
        cmd = ["java", "-cp", self.ant_cp, "org.apache.tools.ant.launch.Launcher",
               "-q", "-f", str(self.root / "build.xml"), *[f"-D{k}={v}" for k, v in defines.items()], *targets]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)

    def decide(self, props: dict[str, str] | None = None) -> Decision:
        done = self.ant("inputs", "report", props=props)
        assert done.returncode == 0, f"Ant failed:\n{done.stdout}\n{done.stderr}"
        kv = dict(line.split("=", 1) for line in (self.root / "out" / "report.txt").read_text().splitlines())
        return Decision(skip={k: kv[f"{k}_skip"] == "true" for k in ("main", "test")},
                        hash={k: kv[f"{k}_hash"] for k in ("main", "test")})

    # the fake codegen -------------------------------------------------------
    def inputs_digest(self, kind: str) -> str:
        files = sorted((self.root / "src" / "main" / "resources" / "db").rglob("*.xml"))
        if kind == "test":
            files += sorted((self.root / "src" / "test" / "resources" / "db").rglob("*.xml"))
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
        (self.gen_dir[kind] / "B.java").write_text("class B {}", encoding="utf-8")
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
                done = self.ant(f"stamp-{kind}", props={**(props or {}), f"nexus.jooq.{kind}.hash": d.hash[kind]})
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


@pytest.fixture()
def proj(tmp_path: Path, ant_classpath: str) -> Project:
    root = tmp_path.resolve() / "service"
    p = Project(root=root, ant_cp=ant_classpath)
    root.mkdir()
    (root / "build.xml").write_text(_build_xml(), encoding="utf-8")
    p.put("pom.xml", "<project>A</project>")
    p.put("src/main/resources/db/changelog/master.xml", "<main>A</main>")
    p.put("src/main/resources/db/changelog/other.xml", "<main-other>A</main-other>")
    p.put("src/test/resources/db/changelog-test/test-master.xml", "<test>A</test>")
    # an unrelated file under resources/ that the hash must NOT cover
    p.put("src/main/resources/META-INF/nexus/release.properties", "stamp=1")
    return p


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


@pytest.mark.parametrize("prop", ["jooq.version", "postgresql.version", "testcontainers.version"])
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
