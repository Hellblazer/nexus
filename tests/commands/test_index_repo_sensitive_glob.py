# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 Step 3.7 (nexus-rfvyy.8.8, Q8): ``nx index repo --sensitive-glob PATTERN``.

Files whose repo-relative path matches a pattern register sensitive, before anything is embedded, and then
follow the same external rules as a catalog-flagged document in a repo run (a Voyage collection skips the
file loudly; a local embedder indexes it on this machine; a PDF goes to the local MinerU subprocess). A file
the catalog did not confirm flagged is not indexed. Files no pattern matches are registered as before.

The mode-lint exemption: the Voyage token appears in no test body here; the external leg is driven by faking
``is_external_embedder``. The engine tests run a real catalog and, for the end-to-end ones, a real
``nx index repo`` on the session engine in local mode (bge), as ``tests/test_scenario_journeys.py`` does.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.sensitive_write import sensitive_glob_matches
from tests._catalog_fixture_ops import documents_by_file_path


# --------------------------------------------------------------------------- #
# The matcher.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("rel", "globs", "expected"),
    [
        pytest.param("deploy/keys/a.pem", ["*.pem"], True, id="name-pattern-any-depth"),
        pytest.param("a.pem", ["*.pem"], True, id="name-pattern-top-level"),
        pytest.param("deploy/keys/id_rsa", ["id_rsa"], True, id="bare-name-at-depth"),
        pytest.param("deploy/keys/id_rsa.pub", ["id_rsa"], False, id="bare-name-is-exact"),
        pytest.param("deploy/keys/a.pem.bak", ["*.pem"], False, id="name-pattern-is-not-a-prefix"),
        pytest.param("secrets/a/b/c.txt", ["secrets/*"], True, id="star-crosses-slash"),
        pytest.param("src/secrets/c.txt", ["secrets/*"], False, id="slash-pattern-anchors-at-the-root"),
        pytest.param("src/secrets/c.txt", ["*/secrets/*"], True, id="leading-star-slash"),
        pytest.param("secrets/c.txt", ["./secrets/*"], True, id="dot-slash-prefix-is-dropped"),
        pytest.param(r"secrets\c.txt", ["secrets/*"], True, id="windows-separators"),
        pytest.param(".env", ["*.env", ".env"], True, id="hidden-file-by-name"),
        pytest.param("src/a.py", ["*.pem", "secrets/*"], False, id="no-match"),
        pytest.param("src/a.py", [], False, id="no-patterns"),
        pytest.param("SECRETS/a.txt", ["secrets/*"], False, id="case-sensitive"),
        # gitignore-like shapes (nexus-rfvyy.8, review r1 S1/S3): '**/' is zero or more directories,
        # a directory name or a trailing slash is the whole subtree.
        pytest.param("a.pem", ["**/*.pem"], True, id="globstar-slash-matches-top-level"),
        pytest.param("deploy/keys/a.pem", ["**/*.pem"], True, id="globstar-slash-matches-deep"),
        pytest.param("a.pem.bak", ["**/*.pem"], False, id="globstar-slash-still-needs-the-suffix"),
        pytest.param("secrets/x", ["secrets/**/x"], True, id="globstar-between-matches-zero-dirs"),
        pytest.param("secrets/a/b/x", ["secrets/**/x"], True, id="globstar-between-matches-many-dirs"),
        pytest.param("secrets/a/y", ["secrets/**/x"], False, id="globstar-between-needs-the-tail"),
        pytest.param("other/secrets/x", ["secrets/**/x"], False, id="globstar-between-is-anchored"),
        pytest.param("secrets/a/b.txt", ["secrets/**"], True, id="trailing-globstar-is-the-subtree"),
        pytest.param("src/secrets/c.txt", ["secrets"], True, id="bare-dir-name-matches-its-subtree-at-depth"),
        pytest.param("secrets/c.txt", ["secrets"], True, id="bare-dir-name-top-level"),
        pytest.param("secrets", ["secrets"], True, id="bare-name-matches-a-file-of-that-name"),
        pytest.param("mysecrets/c.txt", ["secrets"], False, id="bare-dir-name-is-exact"),
        pytest.param("src/secrets/c.txt", ["secrets/"], True, id="trailing-slash-matches-the-subtree-at-depth"),
        pytest.param("secrets/a/b/c.txt", ["secrets/"], True, id="trailing-slash-deep-file"),
        pytest.param("secrets", ["secrets/"], False, id="trailing-slash-is-directories-only"),
        pytest.param("conf/secrets/k", ["conf/secrets/"], True, id="anchored-dir-with-trailing-slash"),
        pytest.param("x/conf/secrets/k", ["conf/secrets/"], False, id="anchored-dir-does-not-float"),
        pytest.param("secrets/c.txt", ["/secrets"], True, id="leading-slash-anchors-at-the-root"),
        pytest.param("a/secrets/c.txt", ["/secrets"], False, id="leading-slash-pins-the-name-to-the-root"),
        pytest.param("a/b.pem", ["b.[pq]em"], True, id="char-class"),
        pytest.param("a/b.pem", ["b.[!p]em"], False, id="negated-char-class"),
        pytest.param("a/b.pem", ["b.?em"], True, id="question-mark"),
    ],
)
def test_the_matcher(rel: str, globs: list[str], expected: bool) -> None:
    assert sensitive_glob_matches(rel, globs) is expected


def test_hits_name_every_pattern_that_matched() -> None:
    from nexus.sensitive_write import sensitive_glob_hits

    globs = ["*.pem", "secrets/", "docs/**/x.md", "nothing-here"]
    assert sensitive_glob_hits("secrets/a.pem", globs) == [0, 1]
    assert sensitive_glob_hits("docs/x.md", globs) == [2]
    assert sensitive_glob_hits("src/a.py", globs) == []
    assert sensitive_glob_hits("", globs) == []


# --------------------------------------------------------------------------- #
# The CLI option.
# --------------------------------------------------------------------------- #

@pytest.fixture
def repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "myrepo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


def _invoke(args: list[str], *, probe=lambda what: None, stats: dict | None = None):
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}
    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.sensitive_write.require_sensitive_engine", probe), \
            patch("nexus.indexer.index_repository", return_value=stats or {}) as idx:
        result = CliRunner().invoke(main, ["index", "repo", *args])
    return result, idx


@pytest.mark.t2_tenant  # a run that reaches the (faked) indexer ends in storage touches: taxonomy marker, engine uptime
def test_the_option_reaches_index_repository_and_without_it_the_call_is_as_before(repo_dir: Path) -> None:
    probes: list[str] = []
    result, idx = _invoke(
        [str(repo_dir), "--sensitive-glob", "*.pem", "--sensitive-glob", "secrets/*"],
        probe=lambda what: probes.append(what))
    assert result.exit_code == 0, result.output
    assert idx.call_args.kwargs["sensitive_globs"] == ("*.pem", "secrets/*")
    assert probes == ["nx index repo --sensitive-glob"]

    probes.clear()
    result, idx = _invoke([str(repo_dir)], probe=lambda what: probes.append(what))
    assert result.exit_code == 0, result.output
    assert "sensitive_globs" not in idx.call_args.kwargs
    assert probes == [], "no option, no engine probe"


def test_an_engine_that_cannot_hold_the_flag_refuses_before_anything_runs(repo_dir: Path) -> None:
    seen: list[str] = []

    def refuse(what: str) -> None:
        seen.append(what)
        raise RuntimeError(f"{what} refused: no catalog_sensitivity_supported. Nothing was written.")

    result, idx = _invoke([str(repo_dir), "--sensitive-glob", "*.pem"], probe=refuse)
    assert result.exit_code != 0, result.output
    assert "Nothing was written" in result.output
    assert seen == ["nx index repo --sensitive-glob"]
    idx.assert_not_called()


def test_the_option_with_frecency_only_is_a_usage_error(repo_dir: Path) -> None:
    result, idx = _invoke([str(repo_dir), "--frecency-only", "--sensitive-glob", "*.pem"])
    assert result.exit_code == 2 and "--frecency-only" in result.output, result.output
    idx.assert_not_called()


@pytest.mark.t2_tenant  # one scenario, one tenant: each run reaches the (faked) indexer and ends in storage touches
def test_the_run_prints_a_count_per_pattern_and_warns_loudly_on_a_pattern_that_matched_nothing(
    repo_dir: Path,
) -> None:
    """A sweep that found nothing is a failure (nexus-moht0): a typo'd pattern would otherwise index the files
    it was meant to protect as ordinary and say nothing."""
    result, _ = _invoke(
        [str(repo_dir), "--sensitive-glob", "*.pem", "--sensitive-glob", "secrts/"],
        stats={"sensitive_glob_counts": {"*.pem": 3, "secrts/": 0}},
    )
    assert result.exit_code == 0, result.output
    assert "*.pem: 3 file(s)" in result.output and "secrts/: 0 file(s)" in result.output, result.output
    warnings = [ln for ln in result.output.splitlines() if ln.startswith("WARNING:")]
    assert len(warnings) == 1 and "'secrts/'" in warnings[0] and "matched 0 files" in warnings[0], result.output
    assert "NOT flagged" in warnings[0]
    assert "--since-head" not in warnings[0], "the delta explanation is only for a --since-head run"

    # a --since-head run says why a pattern may have matched nothing
    result, _ = _invoke(
        [str(repo_dir), "--since-head", "--sensitive-glob", "*.pem"],
        stats={"sensitive_glob_counts": {"*.pem": 0}},
    )
    assert result.exit_code == 0, result.output
    (warning,) = [ln for ln in result.output.splitlines() if ln.startswith("WARNING:")]
    assert "--since-head visits only the files changed" in warning

    # control: every pattern matched, no warning; no option, no report
    result, _ = _invoke([str(repo_dir), "--sensitive-glob", "*.pem"], stats={"sensitive_glob_counts": {"*.pem": 1}})
    assert "*.pem: 1 file(s)" in result.output and "WARNING" not in result.output, result.output
    result, _ = _invoke([str(repo_dir)])
    assert "--sensitive-glob matches" not in result.output, "no option, no report"


def test_the_help_states_the_matching_rule_and_the_external_rule() -> None:
    result = CliRunner().invoke(main, ["index", "repo", "--help"])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "--sensitive-glob PATTERN" in flat
    assert "file and directory name at any depth" in flat and "matches across '/'" in flat
    assert "'**/' matches zero or more directories" in flat
    assert "how many files each pattern matched" in flat and "matched none" in flat
    assert "--since-head only the files the delta visits are flagged" in flat
    assert "skipped with a warning" in flat and "local MinerU" in flat


# --------------------------------------------------------------------------- #
# The catalog hook, against the real engine catalog.
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_document_is_sensitive():
    """Redefined by name, as tests/test_aspect_sensitivity_gates.py does: the suite-wide blanket reads a catalog
    that holds no sensitive document, and this file's subject is the flag the gates read from the real one."""
    yield


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
                 ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "test@test.invalid")]:
        monkeypatch.setenv(k, v)


def _write(root: Path, rel: str, text: str = "x = 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _hook(repo: Path, files: list[Path], globs: tuple[str, ...], *, head: str = "h1"):
    from nexus.indexer import _catalog_hook  # noqa: PLC0415 — heavy module, deferred to the test that needs it

    wanted: set[Path] = set()
    confirmed: set[Path] = set()
    ids = _catalog_hook(
        repo=repo, repo_name=repo.name, repo_hash="rfvyy88000", head_hash=head,
        indexed_files=[(f, "code", "code__rfvyy88") for f in files],
        walk="partial", sensitive_globs=globs, sensitive_wanted=wanted, sensitive_confirmed=confirmed,
    )
    return ids, wanted, confirmed


def _flag(rel: str) -> bool:
    (doc,) = documents_by_file_path(rel)
    return doc.sensitive is True


@pytest.mark.t2_tenant
def test_the_hook_flags_new_and_existing_documents_and_leaves_the_rest(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    a = _write(repo, "keys/a.pem")        # exists unflagged before the glob is given, and does not change
    b = _write(repo, "src/b.py")          # never matches
    _hook(repo, [a, b], ())
    assert not _flag("keys/a.pem") and not _flag("src/b.py")

    c = _write(repo, "keys/c.pem")        # new and matching
    ids, wanted, confirmed = _hook(repo, [a, b, c], ("*.pem",), head="h2")

    assert _flag("keys/a.pem"), "an unchanged existing document that newly matches must be flagged"
    assert _flag("keys/c.pem"), "a new matching document carries the flag in its register request"
    assert not _flag("src/b.py")
    assert wanted == {a, c} and confirmed == {a, c}
    assert set(ids) == {a, b, c}

    # a later run without the option never lowers it
    _hook(repo, [a, b, c], (), head="h3")
    assert _flag("keys/a.pem") and _flag("keys/c.pem")


@pytest.mark.t2_tenant
def test_flagging_an_existing_document_relabels_its_topics_and_a_failed_flag_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-243 Phase 3 fix r3 (N1): a topic built before the glob matched keeps a label computed from the file, so
    the re-flag of an EXISTING document relabels its topics, after the page that flagged it and only for it."""
    from nexus import sensitive_topics  # noqa: PLC0415 — the module the hook resolves the relabel from
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — the class a test patches

    repo = tmp_path / "r"
    a = _write(repo, "keys/a.pem")
    b = _write(repo, "src/b.py")
    _hook(repo, [a, b], ())
    (doc_a,) = documents_by_file_path("keys/a.pem")

    calls: list[list[str]] = []
    monkeypatch.setattr(sensitive_topics, "withhold_after_flag", lambda cat, tumblers: calls.append(list(tumblers)) or [1])
    _hook(repo, [a, b], ("*.pem",), head="h2")
    assert _flag("keys/a.pem") and not _flag("src/b.py")
    assert calls == [[str(doc_a.tumbler)]], "the flagged existing document, and no other, is relabelled"

    # a relabel that raises does not undo the flag or fail the run
    def boom(cat, tumblers):
        raise RuntimeError("taxonomy down")

    monkeypatch.setattr(sensitive_topics, "withhold_after_flag", boom)
    c = _write(repo, "keys/c.pem")
    _hook(repo, [a, b, c], (), head="h3")   # c registers unflagged
    assert not _flag("keys/c.pem")
    _, wanted, confirmed = _hook(repo, [a, b, c], ("*.pem",), head="h4")
    assert _flag("keys/c.pem") and c in confirmed

    # a flag that could not be set relabels nothing
    d = _write(repo, "keys/d.pem")
    _hook(repo, [a, b, c, d], (), head="h5")
    calls.clear()
    monkeypatch.setattr(sensitive_topics, "withhold_after_flag", lambda cat, tumblers: calls.append(list(tumblers)) or [1])

    def refuse(self, tumblers, sensitive, *, reason=""):
        raise RuntimeError("audited route down")

    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", refuse)
    _hook(repo, [a, b, c, d], ("*.pem",), head="h6")
    assert calls == [] and not _flag("keys/d.pem")


@pytest.mark.t2_tenant
def test_the_per_file_fallback_carries_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.catalog.factory as factory  # noqa: PLC0415 — patched at the module the hook resolves it from

    real_writer = factory.make_catalog_writer

    class _NoBatch:
        def __init__(self, target):
            self._target = target

        def __getattr__(self, name):
            return getattr(self._target, name)

        def register_many(self, owner, docs, **kw):
            raise RuntimeError("batch endpoint down")

    monkeypatch.setattr(factory, "make_catalog_writer", lambda *a, **k: _NoBatch(real_writer(*a, **k)))
    repo = tmp_path / "r"
    a = _write(repo, "keys/a.pem")
    b = _write(repo, "src/b.py")
    _, wanted, confirmed = _hook(repo, [a, b], ("*.pem",))
    assert _flag("keys/a.pem") and not _flag("src/b.py")
    assert wanted == confirmed == {a}


@pytest.mark.t2_tenant
def test_a_flag_that_could_not_be_set_is_wanted_but_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — the class a test patches

    repo = tmp_path / "r"
    a = _write(repo, "keys/a.pem")
    _hook(repo, [a], ())

    def boom(self, tumblers, sensitive, *, reason=""):
        raise RuntimeError("audited route down")

    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", boom)
    _, wanted, confirmed = _hook(repo, [a], ("*.pem",), head="h2")
    assert wanted == {a} and confirmed == set()
    assert not _flag("keys/a.pem")


# --------------------------------------------------------------------------- #
# A real `nx index repo` on the session engine (local mode, bge embedder).
# --------------------------------------------------------------------------- #

def _git_init(repo: Path) -> None:
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "test@nexus"],
                ["git", "config", "user.name", "Nexus Test"], ["git", "add", "."], ["git", "commit", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


_SECRET = "def rotate_vault_credentials(token):\n    \"\"\"Rotate the harbour vault credentials with the escrow token.\"\"\"\n    return token[::-1]\n"
_PLAIN = "def next_power_of_two(n):\n    \"\"\"Round n up to the next power of two for ring-buffer sizing.\"\"\"\n    p = 1\n    while p < n:\n        p *= 2\n    return p\n"


def _hits(query: str) -> str:
    """The code-corpus hits for *query* as one JSON text (title, collection and content included)."""
    out = CliRunner().invoke(main, ["search", query, "--corpus", "code", "--json"])
    assert out.exit_code == 0, out.output
    return json.dumps(json.loads(out.stdout))


def _two_file_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "glob-repo"
    repo.mkdir(parents=True)
    (repo / "vault_secret.py").write_text(_SECRET)
    (repo / "ring_buffer.py").write_text(_PLAIN)
    _git_init(repo)
    return repo


def test_a_matched_file_is_indexed_on_this_machine_and_skipped_for_an_external_embedder(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One journey, two repos: the Step 3c rules of a repo run. With a local embedder the matched file is
    flagged and indexed on this machine. With the collection faked external (no --ack-external-embedder
    exists in a repo run) the flagged file is skipped, loudly, and the other file is not gated. The first
    half is the positive control for the second: the same query finds the same content when not skipped."""
    local = _two_file_repo(tmp_path / "local")
    out = CliRunner().invoke(
        main, ["index", "repo", str(local), "--no-taxonomy", "--sensitive-glob", "vault_*", "--sensitive-glob", "vualt_*"])
    assert out.exit_code == 0, out.output
    assert _flag("vault_secret.py") and not _flag("ring_buffer.py")
    # the walk's own count, per pattern, and a loud warning for the typo'd one (nexus-rfvyy.8 review r1 S1/S3)
    assert "vault_*: 1 file(s)" in out.output and "vualt_*: 0 file(s)" in out.output, out.output
    assert "WARNING: --sensitive-glob 'vualt_*' matched 0 files" in out.output, out.output
    assert "WARNING: --sensitive-glob 'vault_*'" not in out.output
    assert "escrow" in _hits("rotate harbour vault credentials escrow token")
    assert "ring-buffer" in _hits("round up to next power of two ring buffer sizing")

    monkeypatch.setattr("nexus.sensitive_write.is_external_embedder", lambda name: True)
    ext = tmp_path / "external" / "other-repo"
    ext.mkdir(parents=True)
    (ext / "vault_ledger.py").write_text(
        "def seal_amber_ledger():\n    \"\"\"Seal the amber ledger with the saffron escrow.\"\"\"\n    return 2\n")
    (ext / "plain.py").write_text("def plain_helper():\n    \"\"\"A plain helper for tangerine sorting.\"\"\"\n    return 3\n")
    _git_init(ext)
    out = CliRunner().invoke(main, ["index", "repo", str(ext), "--no-taxonomy", "--sensitive-glob", "vault_*"])
    assert out.exit_code == 0, out.output
    assert _flag("vault_ledger.py")
    assert "saffron" not in _hits("seal the amber ledger with the saffron escrow")
    assert "tangerine" in _hits("a plain helper for tangerine sorting")


def test_a_matched_file_whose_flag_could_not_be_set_is_not_indexed(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — the class a test patches

    repo = _two_file_repo(tmp_path)
    first = CliRunner().invoke(main, ["index", "repo", str(repo), "--no-taxonomy"])
    assert first.exit_code == 0, first.output
    (repo / "vault_secret.py").write_text(_SECRET + "\n\ndef reseal_quartz_ledger():\n    \"\"\"Reseal the quartz ledger.\"\"\"\n    return 1\n")

    real = HttpCatalogClient.set_sensitivity

    def boom(self, tumblers, sensitive, *, reason=""):
        raise RuntimeError("audited route down")

    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", boom)
    out = CliRunner().invoke(main, ["index", "repo", str(repo), "--no-taxonomy", "--sensitive-glob", "vault_*"])
    assert not _flag("vault_secret.py")
    # the changed content of the file the flag could not protect was not embedded
    assert "quartz" not in _hits("reseal the quartz ledger")
    assert "not indexed" in out.output and "--sensitive-glob" in out.output, out.output

    # control: the same content IS indexed once the run is not refused (so the absence above was the refusal)
    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", real)
    again = CliRunner().invoke(main, ["index", "repo", str(repo), "--no-taxonomy", "--sensitive-glob", "vault_*"])
    assert again.exit_code == 0, again.output
    assert _flag("vault_secret.py")
    assert "quartz" in _hits("reseal the quartz ledger")
