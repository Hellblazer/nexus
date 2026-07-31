# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-u7r0: ``nx t3 prune-stale`` subcommand.

RDR-090 P1.4. Tests use a real T3Database backed by chromadb's
EphemeralClient + DefaultEmbeddingFunction so we exercise the full
delete-by-source machinery without Cloud credentials.

nexus-bm8dd (2026-07-31): the VERB is RETIRED. It swept chunks by their
``source_path`` metadata, and RDR-102 D2 hard-removed that key from the chunk
schema — ``make_chunk_metadata`` raises ``TypeError`` if a caller passes it — so
the sweep matched nothing on any collection the product actually writes and
reported a clean "0 stale" regardless. The five CLI tests that pinned its
reporting and deletion behaviour are replaced by pins on the retirement itself.

The three ``list_unique_source_paths`` tests below survive and still pass, but
read them for what they are: they exercise ``T3Database`` (the legacy Chroma
class, which ``make_t3()`` has not returned since nexus-i711w) against chunks
whose ``source_path`` this file writes BY HAND via ``col.add``. They prove the
Chroma method does what it says; they are not evidence that any chunk in a real
collection carries the key. They go with the Chroma leg in the 7.0.0 wave.

Contracts pinned here:

  - ``nx t3 prune-stale`` exits non-zero, names the bead, and names the
    replacement pipeline instead of running a sweep that cannot work.
  - ``list_unique_source_paths`` (T3Database/Chroma only) deduplicates across
    multi-chunk same-source documents and skips empty/missing values.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    source_path: str,
) -> None:
    """Insert one chunk into *collection* with the given metadata.

    Uses the underlying chroma collection ``add`` directly so we don't
    invoke the indexing pipeline. The EphemeralClient + DefaultEF
    handles the embedding inline.
    """
    col = t3_db._client.get_or_create_collection(collection)
    col.add(ids=[chunk_id], documents=[content], metadatas=[{"source_path": source_path}])


# ── list_unique_source_paths (db-level) ───────────────────────────────────


def test_list_unique_source_paths_dedupes_by_source(t3_db, tmp_path):
    """Multiple chunks with the same source_path collapse to one entry."""
    coll = "knowledge__test_dedupe"
    src = str(tmp_path / "doc-a.md")
    _seed_chunk(t3_db, collection=coll, chunk_id="c1", content="a1", source_path=src)
    _seed_chunk(t3_db, collection=coll, chunk_id="c2", content="a2", source_path=src)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c3", content="b1",
        source_path=str(tmp_path / "doc-b.md"),
    )
    paths = t3_db.list_unique_source_paths(coll)
    assert len(paths) == 2
    assert sorted(paths) == sorted([
        str(tmp_path / "doc-a.md"),
        str(tmp_path / "doc-b.md"),
    ])


def test_list_unique_source_paths_skips_empty(t3_db, tmp_path):
    """Chunks with empty source_path (MCP-put) are not returned."""
    coll = "knowledge__test_empty"
    _seed_chunk(t3_db, collection=coll, chunk_id="c1", content="x", source_path="")
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c2", content="y",
        source_path=str(tmp_path / "doc-real.md"),
    )
    paths = t3_db.list_unique_source_paths(coll)
    assert paths == [str(tmp_path / "doc-real.md")]


def test_list_unique_source_paths_missing_collection(t3_db):
    assert t3_db.list_unique_source_paths("knowledge__nonexistent") == []


# ── nx t3 prune-stale CLI (integration via patched make_t3) ───────────────



# ── the verb itself (nexus-bm8dd: retired) ────────────────────────────────


def test_prune_stale_exits_nonzero_instead_of_sweeping(t3_db, tmp_path, runner):
    """The old verb's worst property was that it SUCCEEDED. It printed
    "Summary: would delete 0 chunk(s)" on a corpus with deleted files, which an
    operator reads as "checked, nothing stale" rather than "could not check".

    Seed a chunk whose source file does not exist — the exact input the sweep
    was for — and assert the verb refuses rather than reporting a clean result.
    """
    missing = tmp_path / "gone.md"
    assert not missing.exists()  # non-vacuity: this IS the stale case
    _seed_chunk(
        t3_db, collection="docs__bm8dd", chunk_id="c1",
        content="body of a document whose file was deleted",
        source_path=str(missing),
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "prune-stale"])

    assert result.exit_code != 0, result.output
    # It must NOT look like a completed sweep.
    assert "Summary:" not in result.output
    assert "0 chunk(s)" not in result.output


def test_prune_stale_message_names_the_cause_and_the_replacement(t3_db, runner):
    """A retired verb that does not say what to run instead just moves the
    operator's problem. Pin both halves of the message."""
    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "prune-stale"])

    assert "nexus-bm8dd" in result.output
    assert "source_path" in result.output, "must name WHY it cannot work"
    assert "nx catalog prune-stale" in result.output
    assert "nx t3 gc" in result.output


def test_prune_stale_message_advertises_only_flags_that_exist(t3_db, runner):
    """The remediation text is the whole value of a retired verb, and it shipped
    (1232585d) advertising ``nx catalog prune-stale [-c COLLECTION]`` when that
    command declares only ``--collection``. Copy-pasting it yields
    "Error: No such option: -c" — the identical failure mode the same commit was
    fixing one line below for ``nx t3 gc``.

    Resolve each advertised option against the real Click command rather than
    string-matching, so this cannot rot the way the message did.
    """
    from nexus.cli import main as cli_main

    with patch("nexus.db.make_t3", return_value=t3_db):
        out = runner.invoke(main, ["t3", "prune-stale"]).output

    def opts_of(*path: str) -> set[str]:
        cmd = cli_main
        for part in path:
            cmd = cmd.get_command(None, part)  # type: ignore[union-attr]
            assert cmd is not None, f"advertised command does not exist: {' '.join(path)}"
        # secondary_opts carries the OFF half of a Click boolean pair
        # (``--dry-run/--no-dry-run`` registers only ``--dry-run`` in .opts), and
        # the message legitimately advertises the OFF half.
        return {
            o
            for p in cmd.params
            for o in (*getattr(p, "opts", []), *getattr(p, "secondary_opts", []))
        }

    # The two commands the message tells the operator to run must both exist...
    prune_opts = opts_of("catalog", "prune-stale")
    gc_opts = opts_of("t3", "gc")
    # ...and every flag the message pairs with them must be a real option.
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("nx catalog prune-stale"):
            target = prune_opts
        elif stripped.startswith("nx t3 gc"):
            target = gc_opts
        else:
            continue
        for tok in stripped.split():
            # STRIP FIRST, THEN test. The advertised flag is always bracketed
            # (``[-c`` / ``[--collection``), so testing startswith("-") on the RAW
            # token skipped every optional flag — including the ``-c`` this test
            # exists to catch. Caught in review of 351874c5: the mutation appeared
            # to fail only because the ``--collection`` string-match below fired,
            # not because the resolution machinery ran. A mutation going red tells
            # you SOMETHING caught it, not WHICH thing did.
            flag = tok.strip("[],")
            if flag.startswith("-"):
                assert flag in target, (
                    f"{stripped!r} advertises {flag!r}, which is not an option of "
                    f"that command (real options: {sorted(target)})"
                )

    # Non-vacuity: the loop above must actually have inspected the prune-stale
    # line, not silently matched nothing.
    assert "nx catalog prune-stale" in out
    assert "--collection" in out, "the prune-stale line must name its real long flag"

    # The enumeration one-liner must use the COLLECTION enumerator. `nx catalog
    # list` lists documents and emits no collection name, so the loop the message
    # used to print GC'd nothing and said nothing.
    if "xargs" in out:
        assert "nx collection list" in out
        assert "nx catalog list" not in out


def test_prune_stale_refuses_under_every_flag_combination(t3_db, runner):
    """Including the destructive one. --no-dry-run --confirm previously deleted;
    it must not now silently succeed as a no-op."""
    for argv in (
        ["t3", "prune-stale", "--no-dry-run"],
        ["t3", "prune-stale", "--no-dry-run", "--confirm"],
        ["t3", "prune-stale", "-c", "docs__bm8dd", "--no-dry-run", "--confirm"],
    ):
        with patch("nexus.db.make_t3", return_value=t3_db):
            result = runner.invoke(main, argv)
        assert result.exit_code != 0, f"{argv} -> {result.output}"
        assert "RETIRED" in result.output
