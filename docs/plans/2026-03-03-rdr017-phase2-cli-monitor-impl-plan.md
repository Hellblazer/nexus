# Implementation Plan: RDR-017 Phase 2 — CLI `--monitor` flag + tqdm bar

**Date:** 2026-03-03
**Parent bead:** nexus-p0me (RDR-017 Phase 2: CLI integration)
**Design doc:** `docs/plans/2026-03-03-rdr017-phase2-cli-monitor-design.md`

## Executive Summary

Wire tqdm progress bars and a `--monitor` flag into all four `nx index`
subcommands (`repo`, `rdr`, `pdf`, `md`). Phase 1 delivered the indexer-layer
callback hooks (`on_start`, `on_file`, `return_metadata`); this phase connects
them to the CLI with user-visible output.

**Scope:** 1 source file (`src/nexus/commands/index.py`), 2 test files
(`tests/test_index_cmd.py`, `tests/test_index_rdr_cmd.py`).
11 new test functions (14 pytest items due to parametrize), 21 existing tests must pass. Total: ~35 pytest items.

## Dependency Graph

```
nexus-lzs9  Task 1: flag + imports (foundation)
    |
    +---> nexus-hqou  Task 2: index_repo_cmd closures + 5 tests
    |
    +---> nexus-4val  Task 3: index_rdr_cmd bar + closure + 3 tests
    |
    +---> nexus-ype6  Task 4: index_pdf_cmd + index_md_cmd + 2 tests
    |
    v
nexus-vm0z  Task 5: final verification (all 35 pytest items green)
```

Tasks 2, 3, 4 are independent of each other (all depend only on Task 1).
A single implementer should execute them sequentially (2 -> 3 -> 4) since
all modify the same source file. Task 5 is the gate.

## Critical Path

Task 1 -> Task 2 -> Task 5 (longest chain: foundation -> most complex command -> verify)

## Baseline

- 21 tests passing across `tests/test_index_cmd.py` (15) and `tests/test_index_rdr_cmd.py` (6)
- Verified: `uv run python -m pytest tests/test_index_cmd.py tests/test_index_rdr_cmd.py -q` = 21 passed

---

## Task 1: Foundation — flag + imports (nexus-lzs9)

**File:** `src/nexus/commands/index.py`
**Test file:** `tests/test_index_cmd.py`
**Blocks:** nexus-hqou, nexus-4val, nexus-ype6

### Step 1a: Write failing test (TDD)

Add to `tests/test_index_cmd.py` — test spec 1:

```python
# ── --monitor flag ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subcmd,extra_args", [
    ("repo", []),
    ("rdr", []),
    ("pdf", []),
    ("md", []),
])
def test_monitor_flag_accepted(
    runner: CliRunner, index_home: Path, subcmd: str, extra_args: list[str]
) -> None:
    """--monitor flag is accepted by all four index subcommands (exit 0)."""
    # Create minimal fixtures for each subcommand
    if subcmd == "repo":
        target = index_home / "myrepo"
        target.mkdir()
    elif subcmd in ("pdf", "md"):
        target = index_home / f"doc.{subcmd}"
        target.write_bytes(b"fake")
    else:
        # rdr: needs docs/rdr/ with at least one .md
        target = index_home / "myrepo"
        rdr_dir = target / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "001.md").write_text("# RDR\n")

    mock_target = {
        "repo": "nexus.indexer.index_repository",
        "rdr": "nexus.doc_indexer.batch_index_markdowns",
        "pdf": "nexus.doc_indexer.index_pdf",
        "md": "nexus.doc_indexer.index_markdown",
    }[subcmd]
    mock_rv = {} if subcmd in ("repo", "rdr") else 0

    patches = [patch(mock_target, return_value=mock_rv)]
    if subcmd == "repo":
        mock_reg = MagicMock()
        mock_reg.get.return_value = {"collection": "code__x"}
        patches.append(patch("nexus.commands.index._registry", return_value=mock_reg))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = runner.invoke(main, ["index", subcmd, str(target), "--monitor"] + extra_args)

    assert result.exit_code == 0, f"{subcmd}: {result.output}"
```

Note: requires `import contextlib` at top of test file.

**Run:** `uv run python -m pytest tests/test_index_cmd.py::test_monitor_flag_accepted -v`
**Expected:** FAIL (--monitor not recognized)

### Step 1b: Implement flag + imports

In `src/nexus/commands/index.py`:

1. Add imports after `import click`:
   ```python
   import sys
   from tqdm import tqdm
   ```

2. Add `--monitor` option to all four commands. For each command, add the
   decorator and the `monitor` parameter:

   ```python
   @click.option("--monitor", is_flag=True, default=False,
                 help="Print per-file progress lines (verbose monitoring without debug spam).")
   ```

   Functions become:
   - `index_repo_cmd(path, frecency_only, force, monitor)`
   - `index_rdr_cmd(path, force, monitor)`
   - `index_pdf_cmd(path, corpus, collection, dry_run, force, monitor)`
   - `index_md_cmd(path, corpus, force, monitor)`

   At this step, `monitor` is accepted but unused (no behavior change yet).

**Run:** `uv run python -m pytest tests/test_index_cmd.py tests/test_index_rdr_cmd.py -q`
**Expected:** 25 pytest items pass (21 existing + 4 from parametrized test_monitor_flag_accepted)

### Verification gate
- [ ] `test_monitor_flag_accepted` passes for all 4 subcmds
- [ ] All 21 existing tests still pass

---

## Task 2: index_repo_cmd closures + tests (nexus-hqou)

**File:** `src/nexus/commands/index.py` (lines 37-70)
**Test file:** `tests/test_index_cmd.py`
**Depends on:** nexus-lzs9

### Step 2a: Write failing tests (TDD)

Add to `tests/test_index_cmd.py` — test specs 2, 3, 4, 5, 10:

**Test spec 2: on_start and on_file always passed as callables**
```python
def test_repo_callbacks_always_passed(runner: CliRunner, index_home: Path) -> None:
    """index_repository is called with on_start and on_file callables (even without --monitor)."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert callable(kwargs.get("on_start")), "on_start must be callable"
    assert callable(kwargs.get("on_file")), "on_file must be callable"
```

**Test spec 3: --monitor non-TTY output contains [N/total]**
```python
def test_repo_monitor_nontty_output_format(runner: CliRunner, index_home: Path) -> None:
    """With --monitor in non-TTY, output contains [N/total] lines."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_file = kwargs.get("on_file")
        if on_start:
            on_start(2)
        if on_file:
            on_file(Path("a.py"), 5, 0.1)
            on_file(Path("b.py"), 0, 0.05)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "[1/2]" in result.output
    assert "[2/2]" in result.output
```

**Test spec 4: chunks=0 -> "skipped"**
```python
def test_repo_monitor_skipped_label(runner: CliRunner, index_home: Path) -> None:
    """on_file with chunks=0 produces 'skipped' in monitor output."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        if kwargs.get("on_start"):
            kwargs["on_start"](1)
        if kwargs.get("on_file"):
            kwargs["on_file"](Path("skip.py"), 0, 0.02)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
```

**Test spec 5: chunks>0 -> "chunks"**
```python
def test_repo_monitor_chunks_label(runner: CliRunner, index_home: Path) -> None:
    """on_file with chunks>0 produces 'chunks' in monitor output."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        if kwargs.get("on_start"):
            kwargs["on_start"](1)
        if kwargs.get("on_file"):
            kwargs["on_file"](Path("code.py"), 7, 0.3)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "7 chunks" in result.output
```

**Test spec 10: non-TTY output has no \\r**
```python
def test_repo_monitor_nontty_no_cr(runner: CliRunner, index_home: Path) -> None:
    """Non-TTY monitor output contains no carriage return characters."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        if kwargs.get("on_start"):
            kwargs["on_start"](1)
        if kwargs.get("on_file"):
            kwargs["on_file"](Path("f.py"), 3, 0.1)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "\r" not in result.output
```

**Run:** `uv run python -m pytest tests/test_index_cmd.py -k "repo_callbacks or repo_monitor" -v`
**Expected:** 5 new tests FAIL (callbacks not yet passed, no monitor output)

### Step 2b: Implement on_start/on_file closures

Modify `index_repo_cmd` in `src/nexus/commands/index.py`:

```python
def index_repo_cmd(path: Path, frecency_only: bool, force: bool, monitor: bool) -> None:
    from nexus.indexer import index_repository

    if force and frecency_only:
        raise click.UsageError("--force and --frecency-only are mutually exclusive.")

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    label = "Force-indexing" if force else ("Updating frecency scores" if frecency_only else "Indexing")
    click.echo(f"{label} {path}...")

    bar = None
    n = 0
    total = 0

    def on_start(count: int) -> None:
        nonlocal bar, total
        total = count
        bar = tqdm(total=count, disable=None, desc=path.name, unit="file")

    def on_file(fpath: Path, chunks: int, elapsed: float) -> None:
        nonlocal n
        n += 1
        if bar is not None:
            bar.update(1)
            bar.set_postfix(now=fpath.name)
        if monitor:
            lbl = f"{chunks} chunks" if chunks else "skipped"
            line = f"  [{n}/{total}] {fpath.name} \u2014 {lbl}  ({elapsed:.1f}s)"
            if bar is not None and sys.stdout.isatty():
                tqdm.write(line)
            else:
                click.echo(line)

    stats = index_repository(path, reg, frecency_only=frecency_only, force=force,
                             on_start=on_start, on_file=on_file)
    if bar:
        bar.close()

    # ... rest of stats output unchanged ...
```

**Run:** `uv run python -m pytest tests/test_index_cmd.py -q`
**Expected:** All tests pass (21 existing + 1 parametrized + 5 new = ~30)

### Verification gate
- [ ] 5 new repo tests pass
- [ ] All 21 existing tests still pass
- [ ] `on_start` and `on_file` always passed (even without `--monitor`)

---

## Task 3: index_rdr_cmd bar + on_file + tests (nexus-4val)

**File:** `src/nexus/commands/index.py` (lines 188-226)
**Test files:** `tests/test_index_cmd.py`, `tests/test_index_rdr_cmd.py`
**Depends on:** nexus-lzs9

### Step 3a: Write failing tests (TDD)

**In `tests/test_index_cmd.py` -- test specs 6, 7:**

**Test spec 6: batch_index_markdowns called with on_file kwarg**
```python
def test_rdr_monitor_on_file_passed(runner: CliRunner, index_home: Path) -> None:
    """With --monitor, batch_index_markdowns is called with on_file kwarg."""
    repo = index_home / "myrepo"
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001.md").write_text("# RDR\n")

    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_batch.call_args
    assert callable(kwargs.get("on_file")), "on_file must be callable"
```

**Test spec 7: bar created with total=len(rdr_files)**
```python
def test_rdr_monitor_bar_total(runner: CliRunner, index_home: Path) -> None:
    """tqdm bar is created with total=len(rdr_files)."""
    repo = index_home / "myrepo"
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001.md").write_text("# A\n")
    (rdr_dir / "002.md").write_text("# B\n")
    (rdr_dir / "003.md").write_text("# C\n")

    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        with patch("nexus.commands.index.tqdm") as mock_tqdm:
            mock_tqdm.return_value = MagicMock()  # mock bar object
            result = runner.invoke(main, ["index", "rdr", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    mock_tqdm.assert_called_once()
    _, tqdm_kwargs = mock_tqdm.call_args
    assert tqdm_kwargs.get("total") == 3 or mock_tqdm.call_args[1].get("total") == 3
```

Note: test spec 7 mocks `tqdm` itself to verify the `total` kwarg. The mock
must be on `nexus.commands.index.tqdm` (the import location).

**In `tests/test_index_rdr_cmd.py` -- test spec 11:**

```python
def test_index_rdr_monitor_flag_and_on_file(
    runner: CliRunner, repo_with_rdrs: Path
) -> None:
    """--monitor flag accepted; on_file callback passed to batch_index_markdowns."""
    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(repo_with_rdrs), "--monitor"])

    assert result.exit_code == 0, result.output
    mock_batch.assert_called_once()
    _, kwargs = mock_batch.call_args
    assert callable(kwargs.get("on_file")), "on_file must be passed when --monitor is set"
```

**Run:** `uv run python -m pytest tests/test_index_cmd.py -k "rdr_monitor" tests/test_index_rdr_cmd.py -k "monitor" -v`
**Expected:** 3 new tests FAIL

### Step 3b: Implement bar + on_file closure

Modify `index_rdr_cmd` in `src/nexus/commands/index.py`:

```python
def index_rdr_cmd(path: Path, force: bool, monitor: bool) -> None:
    from nexus.doc_indexer import batch_index_markdowns
    from nexus.registry import _repo_identity, _rdr_collection_name

    path = path.resolve()
    rdr_dir = path / "docs" / "rdr"

    if not rdr_dir.is_dir():
        click.echo("No docs/rdr/ directory found")
        return

    rdr_files = sorted(
        p for p in rdr_dir.glob("*.md")
        if p.is_file() and p.name not in _RDR_EXCLUDES
    )

    if not rdr_files:
        click.echo("0 RDR documents found.")
        return

    basename, _ = _repo_identity(path)
    collection = _rdr_collection_name(path)
    label = "Force re-indexing" if force else "Indexing"
    click.echo(f"{label} {len(rdr_files)} RDR document(s) into {collection}...")

    bar = tqdm(total=len(rdr_files), disable=None, desc="RDR", unit="doc")
    n = 0

    def on_file(fpath: Path, chunks: int, elapsed: float) -> None:
        nonlocal n
        n += 1
        bar.update(1)
        bar.set_postfix(now=fpath.name)
        if monitor:
            lbl = f"{chunks} chunks" if chunks else "skipped"
            line = f"  [{n}/{len(rdr_files)}] {fpath.name} \u2014 {lbl}  ({elapsed:.1f}s)"
            if sys.stdout.isatty():
                tqdm.write(line)
            else:
                click.echo(line)

    results = batch_index_markdowns(rdr_files, corpus=basename,
                                     collection_name=collection, force=force,
                                     on_file=on_file)
    bar.close()

    indexed = sum(1 for s in results.values() if s == "indexed")
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {indexed} of {len(rdr_files)} RDR document(s).")
```

**Run:** `uv run python -m pytest tests/test_index_cmd.py tests/test_index_rdr_cmd.py -q`
**Expected:** All tests pass

### Verification gate
- [ ] 3 new rdr tests pass
- [ ] All 6 existing rdr tests still pass
- [ ] All existing index_cmd tests still pass

---

## Task 4: index_pdf_cmd + index_md_cmd + tests (nexus-ype6)

**File:** `src/nexus/commands/index.py` (lines 99-182)
**Test file:** `tests/test_index_cmd.py`
**Depends on:** nexus-lzs9

### Step 4a: Write failing tests (TDD)

**Test spec 8: index_pdf with --monitor calls return_metadata=True**
```python
def test_pdf_monitor_return_metadata(runner: CliRunner, index_home: Path) -> None:
    """With --monitor, index_pdf is called with return_metadata=True."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    mock_result = {"chunks": 3, "pages": [1, 2, 3], "title": "Test", "author": "Author"}
    with patch("nexus.doc_indexer.index_pdf", return_value=mock_result) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--monitor"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs.get("return_metadata") is True
    assert "Chunks: 3" in result.output
```

**Test spec 9: index_md with --monitor calls return_metadata=True**
```python
def test_md_monitor_return_metadata(runner: CliRunner, index_home: Path) -> None:
    """With --monitor, index_markdown is called with return_metadata=True."""
    md = index_home / "doc.md"
    md.write_text("# Hello\n\nWorld.\n")

    mock_result = {"chunks": 2, "sections": 1}
    with patch("nexus.doc_indexer.index_markdown", return_value=mock_result) as mock_index:
        result = runner.invoke(main, ["index", "md", str(md), "--monitor"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs.get("return_metadata") is True
    assert "Chunks: 2" in result.output
    assert "Sections: 1" in result.output
```

**Run:** `uv run python -m pytest tests/test_index_cmd.py -k "pdf_monitor or md_monitor" -v`
**Expected:** 2 new tests FAIL

### Step 4b: Implement return_metadata branches

**index_pdf_cmd** -- modify the non-dry-run path (lines 157-161):

```python
    if monitor:
        result = index_pdf(path, corpus=corpus, collection_name=collection, force=force,
                           return_metadata=True)
        n = result["chunks"]
        pages = result.get("pages", [])
        page_range = f"{pages[0]}\u2013{pages[-1]}" if len(pages) > 1 else str(pages[0]) if pages else "?"
        title = result.get("title", "")
        author = result.get("author", "")
        parts = [f"Chunks: {n}", f"Pages: {page_range}"]
        if title:
            parts.append(f'Title: "{title}"')
        if author:
            parts.append(f'Author: "{author}"')
        click.echo(f"\n  {'  '.join(parts)}")
    else:
        n = index_pdf(path, corpus=corpus, collection_name=collection, force=force)
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {n} chunk(s).")
```

**index_md_cmd** -- modify the body (lines 178-182):

```python
    if monitor:
        result = index_markdown(path, corpus=corpus, force=force, return_metadata=True)
        n = result["chunks"]
        sections = result.get("sections", 0)
        click.echo(f"\n  Chunks: {n}  Sections: {sections}")
    else:
        n = index_markdown(path, corpus=corpus, force=force)
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {n} chunk(s).")
```

**Run:** `uv run python -m pytest tests/test_index_cmd.py -q`
**Expected:** All tests pass

### Verification gate
- [ ] 2 new pdf/md tests pass
- [ ] All existing pdf/md tests still pass (mock returns int, not dict, so non-monitor path unchanged)

---

## Task 5: Final Verification (nexus-vm0z)

**Depends on:** nexus-hqou, nexus-4val, nexus-ype6

### Step 5a: Run full test suite

```bash
uv run python -m pytest tests/test_index_cmd.py tests/test_index_rdr_cmd.py -v
```

**Expected:** 35 pytest items pass (21 existing + 14 new items: 4 parametrized + 10 individual)

### Step 5b: Verify test count

Count tests in each file:
- `tests/test_index_cmd.py`: 15 existing + 10 new = 25
- `tests/test_index_rdr_cmd.py`: 6 existing + 1 new = 7
- Total: 32

Note: test spec 1 is parametrized x4, so it contributes 4 test cases.
Adjusted count: 15 + 4 + 5 + 2 + 2 = 28 in test_index_cmd.py, plus 7 in
test_index_rdr_cmd.py = 35. But the parametrized test is a single test
function generating 4 cases. The spec says "11 new test specs" so counting
by test functions: 10 new functions in test_index_cmd.py + 1 in
test_index_rdr_cmd.py = 11 functions, but the parametrized one creates 4
pytest items.

### Step 5c: Run broader regression

```bash
uv run python -m pytest tests/ -q --tb=short
```

Ensure no regressions in other test files.

### Verification gate
- [ ] All test_index_cmd.py tests pass
- [ ] All test_index_rdr_cmd.py tests pass
- [ ] No regressions in other test files
- [ ] Close nexus-p0me bead

---

## Test Spec Coverage Matrix

| Spec # | Description | Task | Test function |
|--------|-------------|------|---------------|
| 1 | --monitor flag accepted (all 4) | T1 | `test_monitor_flag_accepted` (parametrized) |
| 2 | repo: on_start/on_file always callable | T2 | `test_repo_callbacks_always_passed` |
| 3 | repo --monitor non-TTY: [N/total] | T2 | `test_repo_monitor_nontty_output_format` |
| 4 | repo on_file chunks=0 -> "skipped" | T2 | `test_repo_monitor_skipped_label` |
| 5 | repo on_file chunks>0 -> "chunks" | T2 | `test_repo_monitor_chunks_label` |
| 6 | rdr --monitor: on_file kwarg | T3 | `test_rdr_monitor_on_file_passed` |
| 7 | rdr bar total=len(rdr_files) | T3 | `test_rdr_monitor_bar_total` |
| 8 | pdf --monitor: return_metadata=True | T4 | `test_pdf_monitor_return_metadata` |
| 9 | md --monitor: return_metadata=True | T4 | `test_md_monitor_return_metadata` |
| 10 | non-TTY: no \r chars | T2 | `test_repo_monitor_nontty_no_cr` |
| 11 | rdr: --monitor + on_file (rdr test) | T3 | `test_index_rdr_monitor_flag_and_on_file` |

## Risk Factors

1. **Existing tests break from new param**: LOW risk. `--monitor` defaults to
   `False`; Click handles missing flags transparently. Existing invocations
   without `--monitor` will not be affected.

2. **tqdm side effects in test**: LOW risk. tqdm is disabled in non-TTY
   (`disable=None`). CliRunner is non-TTY.

3. **Mock complexity for callbacks**: MEDIUM risk. Tests 3-5 use
   `side_effect` on `index_repository` mock to invoke callbacks. Pattern is
   well-established in existing test base.

4. **tqdm mock in test 7**: MEDIUM risk. Mocking `nexus.commands.index.tqdm`
   requires that the import is at module level (not inside the function).
   The implementation adds `from tqdm import tqdm` at module level, so the
   mock target is correct.

## Imports Summary

New imports in `src/nexus/commands/index.py`:
```python
import sys
from tqdm import tqdm
```

New imports in `tests/test_index_cmd.py`:
```python
import contextlib
```
(`MagicMock`, `patch` already imported)

## Files Modified

| File | Lines added (est.) | Description |
|------|--------------------|-------------|
| `src/nexus/commands/index.py` | ~60 | Closures, bar logic, metadata branches |
| `tests/test_index_cmd.py` | ~130 | 10 new test functions |
| `tests/test_index_rdr_cmd.py` | ~15 | 1 new test function |
