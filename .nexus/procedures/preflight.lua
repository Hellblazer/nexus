-- procedure: preflight
-- summary: The checks a branch's diff since base needs, as commands to run; it runs none.
-- args: base:string
-- grants: git.diff_names, git.diff_stat

-- RDR-240 Technical Design 8 (nexus-qo7ie.32). Reads the diff from the merge
-- base of `base` and HEAD, names its change classes from the table below, and
-- returns the commands each class needs, in the order to run them. It runs
-- nothing: the sandbox has no shell, and a heavy check runs at the caller's
-- hand (`heavy`: it takes a lab grant, through lab-remote or a --lab-run
-- wrapper; engine builds are never wrapped in lab-run, AGENTS.md rule 13).
--
-- A class is a row: its patterns (Lua patterns, anchored with ^) and the
-- checks it needs. A path belongs to every class one of whose patterns
-- matches it. A code push is CI's predicate: a path outside the docs class
-- makes it one (.github/workflows/ci.yml, the doc-only fast lane), and a code
-- push needs the push train's standing checks (T2 nexus/push-train rule 1).
-- The docs rows are CI's case arms and the plugin rows the paths the
-- drift-ledger workflow watches; tests pin both mirrors.
--
-- A command that takes changed files asks git for them when it is run
-- (--diff-filter=d), so a deleted or moved path never reaches a tool, and
-- passes them after `--`, so no path reads as an option. A file list in the
-- result (`classes`) is the verb's, deleted paths included.

local args = ...
local base = args.base

-- A shell word: as is when plainly safe, else single-quoted with each '
-- written '\''.
local function quote(s)
  if string.match(s, "^[A-Za-z0-9%./_][A-Za-z0-9%./_%-]*$") then
    return s
  end
  return "'" .. string.gsub(s, "'", "'\\''") .. "'"
end

local function words(list)
  local out = {}
  for _, s in ipairs(list) do
    table.insert(out, quote(s))
  end
  return table.concat(out, " ")
end

local RANGE = quote(base .. "...HEAD")

-- The changed paths git still has under these pathspecs, NUL-separated.
local function live(pathspecs)
  return "git diff -z --no-renames --diff-filter=d --name-only " .. RANGE .. " -- " .. words(pathspecs)
end

local function any(files, pattern)
  for _, f in ipairs(files) do
    if string.find(f, pattern) then
      return true
    end
  end
  return false
end

local RUFF = "ruff@0.15.18"

local function step(check, command, heavy)
  return {check = check, command = command, heavy = heavy}
end

-- Rules cited below as R<n> are the numbered rules of the maintainer's memory
-- file feedback_test_runs_before_push.md (~/.claude/projects/<nexus project>/memory/),
-- numbered in T2 nexus/qo7ie.36-census-feedback_test_runs_before_push (nexus-qo7ie.36).

-- The result contract on every pytest command this procedure emits (R44, R55; the
-- source's piped_test_exit and gate_the_push_on_the_pin sections). A run's output
-- goes to a log with -rf, so a red names itself, and the command passes only when
-- pytest exited 0 AND its summary line (the last line that ends in a duration)
-- reads "N passed" with nothing else but warnings, skips, deselects and xfails.
-- A bare `grep -q passed` is not that: "1 failed, 964 passed" matches it, and an
-- empty filter reads as a pass. "no tests ran", a run that printed nothing and a
-- selection deselected by marker all fail it. The summary may carry pytest's "="
-- decoration (a watched_pytest.py run does), and a command that can select
-- nothing (the integration selection) says so on its own line.
local PYTEST = "uv run pytest -q -rf -p no:randomly"
-- Default addopts is -m 'not integration and not slow and not lint', so a step that
-- names a lint-marked file collects nothing from it ("10 deselected") and, with a
-- clean-looking exit, reads green. A step that names files lets a later -m select
-- them whatever their mark (the last -m wins); a test runs pytest --collect-only over
-- each step's own argv and wants a test from every named file.
local NOT_SLOW = "-m 'not integration and not slow'"
local SUMMARY_AT = " in [0-9.]+s( \\([0-9:]+\\))?( =+)?$"
local SUMMARY_OK = "^(=+ )?[0-9]+ passed(, [0-9]+ (warnings?|skipped|deselected|xfailed|xpassed))* in [0-9.]+s( \\([0-9:]+\\))?( =+)?$"
local NOTHING = "nothing selected"

local function gated(command, may_select_nothing)
  local ok = "printf '%s\\n' \"$s\" | grep -Eq '" .. SUMMARY_OK .. "'"
  if may_select_nothing then
    ok = "{ " .. ok .. " || { [ -z \"$s\" ] && grep -qx '" .. NOTHING .. "' \"$log\"; }; }"
  end
  -- The template names TMPDIR: BSD mktemp with no argument ignores it.
  return "log=$(mktemp \"${TMPDIR:-/tmp}/preflight.XXXXXX\") && { { " .. command .. "; } >\"$log\" 2>&1; rc=$?;"
    .. " s=$(grep -E '" .. SUMMARY_AT .. "' \"$log\" | tail -n 1);"
    .. " [ \"$rc\" -eq 0 ] && " .. ok .. " && printf '%s\\n' \"${s:-" .. NOTHING .. "}\"; }"
    .. " || { tail -n 30 \"$log\"; echo \"red, or no clean pytest summary line; full log: $log\"; false; }"
end

-- R13 (tripwires): a default-loop guard that judges only what its own invocation
-- collected, so it runs in the same invocation as the changed tests. It is
-- tests/test_mode_declarations_are_explicit.py (voyage names in a test without
-- cloud_mode; it reads request.session.items and must not be lint-marked). The
-- source's second guard, tests/test_cli_runner_output_invariant.py, became a
-- lint test (dc2a2cca0), which default addopts deselect, so naming it here would
-- run nothing; the standing lint bucket carries it (a test pins that).
local CHANGED_TEST_GUARDS = {"tests/test_mode_declarations_are_explicit.py"}

local CLASSES = {
  {
    name = "python",
    patterns = {"%.py$", "^pyproject%.toml$", "^uv%.lock$"},
    checks = function(files)
      local out = {}
      -- ruff's rules hold across src/ only, and CI pins its version
      -- (.github/workflows/ci.yml, the ruff lint job: uvx ruff@<v> check src/).
      if any(files, "^src/.*%.py$") then
        table.insert(out, step("ruff check on the changed src/ files",
          live({":(glob)src/**/*.py"}) .. " | xargs -0 -r uvx " .. RUFF .. " check --force-exclude --", false))
      end
      if any(files, "^tests/.*test_[^/]*%.py$") then
        table.insert(out, step("the changed test files, with the default-loop guard that judges only its own invocation",
          gated("{ " .. live({":(glob)tests/**/test_*.py"}) .. "; printf '%s\\0' "
            .. words(CHANGED_TEST_GUARDS) .. "; } | xargs -0 -r " .. PYTEST .. " --"), false))
      end
      -- tests/test_cli_registration.py checks every command module is wired into
      -- cli.py; no test that shares a subject with a command module names it, so a
      -- new or moved module under commands/ or an edit to cli.py reds develop alone.
      if any(files, "^src/nexus/commands/") or any(files, "^src/nexus/cli%.py$") then
        table.insert(out, step("the CLI registration test (every command module is wired into cli.py)",
          gated(PYTEST .. " tests/test_cli_registration.py"), false))
      end
      return out
    end,
  },
  {
    name = "engine",
    patterns = {"^service/"},
    checks = function()
      return {
        step("the engine main suite (on a pool host: --lab-run needs lab-run on PATH)",
          "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dnexus.ddl.suite=main", true),
        step("the engine integration group (on a pool host)",
          "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dnexus.ddl.suite=integration"
            .. " -Dtest.excluded.groups= -Dgroups=integration", true),
      }
    end,
  },
  {
    -- A changeset, or CatalogRepository.java, whose COLLECTION_SCOPED_TABLES the
    -- parity test reads against the changelogs (a physical_collection column went
    -- red at 939625129).
    name = "schema",
    patterns = {"^service/src/main/resources/db/changelog/", "^service/.*/CatalogRepository%.java$"},
    checks = function(files)
      local out = {}
      -- Not lint-marked: a `-m lint` here deselected all 32 of its tests and exited 0.
      if any(files, "^service/src/main/resources/db/changelog/") then
        table.insert(out, step("the changeset validation lint",
          gated(PYTEST .. " tests/test_changelog_validate_precondition_lint.py"), false))
      end
      -- It asks a live Postgres through the engine substrate, so it is heavy and
      -- runs after the gate jar (the jar step comes first for any code push).
      table.insert(out, step("the collection-scoped tables schema parity test (needs the engine substrate)",
        gated(PYTEST .. " tests/catalog/test_collection_scoped_tables_schema_parity.py"), true))
      return out
    end,
  },
  {
    -- The paths the drift-ledger workflow watches
    -- (.github/workflows/plugin-drift-ledger.yml, on.push.paths). The ledger
    -- itself is a standing check of every code push, and every plugin path
    -- is code; the class adds the hook tests when a hook changed.
    name = "plugin",
    patterns = {
      "^conexus/hooks/", "^conexus/commands/", "^conexus/skills/",
      "^conexus/agents/", "^conexus/resources/", "^conexus/mcp/",
      "^conexus/launch/", "^conexus/PENDING_RELEASE%.md$",
      "^conexus/%.mcp%.json$", "^sn/hooks/", "^sn/%.mcp%.json$",
      "^tool%-spill/hooks/", "^tool%-spill/%.claude%-plugin/plugin%.json$",
      "^prose%-edit/skills/", "^prose%-edit/agents/",
      "^prose%-edit/%.claude%-plugin/plugin%.json$",
      "^%.claude%-plugin/marketplace%.json$",
      "^scripts/plugin_ledger%.py$", "^scripts/plugin_channel%.py$",
      "^tests/test_plugin_release_drift_ledger%.py$",
      "^%.github/workflows/plugin%-drift%-ledger%.yml$",
      -- Not a watched path: the hook-skew harness is hooks.json's own e2e, and
      -- the hooks.json pin tests below read what it exercises.
      "^tests/e2e/hook%-cli%-skew/",
    },
    checks = function(files)
      local out = {}
      if any(files, "^conexus/hooks/") then
        table.insert(out, step("the hook tests", gated(PYTEST .. " tests/hooks"), false))
      end
      -- hooks.json's entry count and form are pinned in three files outside
      -- tests/hooks; the hook-floor red came from a new hook entry. Two of them
      -- are lint-marked, so the step selects whatever the mark (NOT_SLOW).
      if any(files, "^conexus/hooks/hooks%.json$") or any(files, "^tests/e2e/hook%-cli%-skew/") then
        table.insert(out, step("the hooks.json pin tests",
          gated(PYTEST .. " " .. NOT_SLOW .. " tests/scripts/test_hook_harness_launcher_form.py"
            .. " tests/test_hook_budgets_pinned_to_hooks_json.py tests/test_hooks_json_verb_release_floor.py"),
          false))
      end
      -- R10 (tripwires): hooks.json has its own tripwire in the upgrade e2e.
      if any(files, "^conexus/hooks/hooks%.json$") then
        table.insert(out, step("the SC8 hooks.json test",
          gated(PYTEST .. " tests/test_upgrade_e2e.py -k SC8"), false))
      end
      return out
    end,
  },
  {
    name = "workflow",
    patterns = {"^%.github/workflows/[^/]+%.yml$", "^%.github/workflows/[^/]+%.yaml$"},
    checks = function()
      return {
        step("actionlint on the changed workflows",
          live({":(glob).github/workflows/*.yml", ":(glob).github/workflows/*.yaml"})
            .. " | xargs -0 -r actionlint --", false),
      }
    end,
  },
  {
    -- A Lua procedure, shipped or the repo's own. Every procedure's tests run:
    -- they are fast, and a procedure's tests are named for it, not found by it.
    name = "procedure",
    -- The two tables are the shipped procedures' inputs. The tests: every
    -- tests/test_*procedure*.py (the shipped-procedures and front-door lints are
    -- lint-marked, hence NOT_SLOW) and test_run_cmd.py; test_mcp_procedure_run.py
    -- builds the procedure_run description from the shipped headers.
    patterns = {
      "^src/nexus/procedures/[^/]+%.lua$", "^%.nexus/procedures/[^/]+%.lua$",
      "^src/nexus/tables/audit%-tags%.toml$", "^src/nexus/tables/rdr%-research%-vocab%.toml$",
    },
    checks = function()
      return {
        step("the procedure tests",
          gated(PYTEST .. " " .. NOT_SLOW .. " tests/test_*procedure*.py tests/test_run_cmd.py"), false),
      }
    end,
  },
  {
    -- CI's doc-only predicate, arm by arm: a path with a directory outside
    -- docs/ and web/ is code there, and so is web/install.*.
    name = "docs",
    patterns = {
      "^docs/", "^web/", "^conexus/CHANGELOG%.md$", "^README%.md$",
      "^CHANGELOG[^/]*$", "^LICENSE[^/]*$",
      -- A changelog fragment is prose one level deep (ci.yml: changelog/unreleased/*.md);
      -- a nested path stays code there, so it matches no row here (nexus-3ceda.2).
      "^changelog/unreleased/[^/]+%.md$",
    },
    except = {"^web/install%."},
    checks = function(files)
      -- The selector exits 1 below its floor; the && stops there instead of
      -- letting an empty list run the whole suite. The list goes through
      -- xargs, which splits it in any shell (zsh does not split an unquoted
      -- $f).
      local out = {
        step("the tests that read docs/",
          gated("f=$(uv run python scripts/select_docs_reader_tests.py --repo .)"
            -- CI's docs lane: -o addopts= so the lint-marked docs scans run too;
            -- integration and slow stay out.
            .. " && printf '%s\\n' \"$f\" | xargs " .. PYTEST .. " -o addopts= " .. NOT_SLOW),
          false),
      }
      -- R37 (Addendum 2026-09-18, Fourth recurrence, Never chain): a bead id
      -- cited in docs/ must be in tests/fixtures/bead_ids.txt. The local lint
      -- bucket passes without it (it resolves through the live beads database
      -- when one is present); CI has none and reads only the manifest. So
      -- refresh, then require the manifest to match HEAD: a diff means the
      -- refreshed manifest is not committed with the doc yet.
      if any(files, "^docs/") then
        table.insert(out, step("the bead ids cited in docs/ are in the manifest (needs bd, so run it where the beads database is)",
          "uv run python scripts/refresh_bead_id_manifest.py && "
            .. "{ git diff --quiet HEAD -- tests/fixtures/bead_ids.txt || "
            .. "{ echo 'the manifest changed: commit tests/fixtures/bead_ids.txt with the doc'; false; }; }",
          false))
      end
      -- CI's docs lane also runs the fragment check (a fragment-only or
      -- CHANGELOG-only push takes that lane, not the lint bucket): every fragment
      -- well formed, no [Unreleased] holding a hand entry.
      if any(files, "^changelog/unreleased/") or any(files, "^CHANGELOG%.md$")
        or any(files, "^conexus/CHANGELOG%.md$") then
        table.insert(out, step("the changelog fragments are well formed and no [Unreleased] holds a hand entry",
          "uv run python scripts/changelog_fold.py check", false))
      end
      return out
    end,
  },
}

-- The push train's standing checks for a code push (rule 1). The gate jar
-- comes first for any push that runs pytest, since pytest refuses to start on
-- a stale one (the docs readers included: some need the engine).
local JAR = step("build the gate jar first, pytest refuses a stale one"
  .. " (a cache miss is a Maven build; in a pool-host shell add --lab-run <class>)",
  "scripts/build-gate-jar.sh", true)

local MARK = "'integration and not lived_in and not keyed_voyage and not mandatory_regression_pin'"

local function standing(select_all)
  local sel = select_all
    and "uv run python scripts/select_affected_integration_tests.py --repo . --all"
    or "git diff --no-renames --name-only " .. RANGE
      .. " | uv run python scripts/select_affected_integration_tests.py --repo ."
  return {
    -- R33 (Third recurrence, 2026-10-15): the repo-wide shell lints enumerate
    -- tracked files, so a new file that is not tracked is never scanned and the
    -- bucket reads green. The command fails, listing them, while any exists.
    step("precondition of the lint bucket: every new file is tracked (git add them; lab-remote sends"
      .. " uncommitted changes unstaged, so commit before a remote run)",
      "u=$(git ls-files --others --exclude-standard) && [ -z \"$u\" ] || "
        .. "{ printf 'untracked files, so the repo-wide lints skip them (git add first):\\n%s\\n' \"$u\"; false; }",
      false),
    -- R32/R59: the whole bucket, never a subset; it also carries
    -- tests/test_cli_runner_output_invariant.py (R13, lint-marked).
    step("the whole lint bucket",
      gated("lab-remote -- bash -c 'scripts/build-gate-jar.sh && " .. PYTEST .. " -m lint -n auto'"), true),
    step("the drift ledger and the lifecycle gate (not lint-marked)",
      gated(PYTEST .. " tests/test_plugin_release_drift_ledger.py tests/daemon/test_lifecycle_gate.py"),
      false),
    step("tests that mint a tenant, at +0 or below",
      "uv run scripts/tenant_delta.py --strict --base=" .. quote(base), false),
    step("whether the change needs a native Windows run (it reads origin/develop...HEAD)",
      "uv run scripts/windows_hand_test.py --check", false),
    -- CI's mark expression (ci.yml INTEGRATION_MARK_EXPR) and its rule that a
    -- change to the selector selects every integration file.
    step("the integration tests the change affects",
      gated("f=$(" .. sel .. ")"
        .. " && { if [ -z \"$f\" ]; then echo '" .. NOTHING .. "'; else printf '%s\\n' \"$f\" | xargs lab-remote -- "
        .. PYTEST .. " -o addopts= -m " .. MARK .. "; fi; }", true),
      true),
  }
end

-- Rows that a diff's paths decide and no class owns: a check fires when `when`
-- holds of the changed paths (the verb's list, deleted paths included). Each is a
-- tripwire the source names for a change class; cheap ones first, the full suite
-- last. Where a rule needs more than paths (removed lines, symbols, the host's OS)
-- it is not here: it is a step of the repo skill .claude/skills/pre-push-review.
local FENCE_GATE_FILES = {
  "^src/nexus/exporter%.py$", "^src/nexus/pipeline_stages%.py$", "^src/nexus/chunk_batcher%.py$",
  "^src/nexus/catalog/recovery_bundle%.py$", "^src/nexus/commands/store%.py$",
  "^src/nexus/commands/memory%.py$", "^src/nexus/mcp/core%.py$",
}

local function any_of(names, patterns)
  for _, p in ipairs(patterns) do
    if any(names, p) then
      return true
    end
  end
  return false
end

local TRIPWIRES = {
  {
    -- R8 (service substrate): a new public method on any Http*Store fails
    -- the frozen-contract parity test, which is not lint-marked. The T2 stores
    -- live in db/t2/http_*.py, the scratch store beside them in db/.
    when = function(names)
      return any_of(names, {"^src/nexus/db/t2/http_[^/]*%.py$", "^src/nexus/db/http_scratch_store%.py$"})
    end,
    step = step("the Http store parity test (every public Http*Store method must be classified in the contract)",
      gated(PYTEST .. " tests/db/test_http_t2_store_parity.py"), false),
  },
  {
    -- R11 (tripwires, new catalog access): the census counts Catalog.is_initialized
    -- in every file under src/nexus, so a path in that tree is the narrowest
    -- trigger a path can name for "new catalog access". It is a superset by design.
    when = function(names)
      return any(names, "^src/nexus/.*%.py$")
    end,
    step = step("the catalog gate census (a new catalog access outside the allowlist fails it)",
      gated(PYTEST .. " tests/test_kmo9h_catalog_gate_census.py"), false),
  },
  {
    -- R31 (Recurred 2026-10-23): the scan of TMPDIR overrides walks every python
    -- file under tests/ and is not lint-marked (a CI shard caught it), so a
    -- changed python file there, or the helper itself, can trip it.
    when = function(names)
      return any(names, "^tests/.*%.py$")
    end,
    step = step("the TMPDIR override scan (any test env carrying TMPDIR goes through tests._tmp_root.child_env_with_tmpdir)",
      gated(PYTEST .. " tests/test_tmp_root.py"), false),
  },
  {
    -- R39 (Third recurrence, 2026-10-15): tests/scripts holds a non-lint census of
    -- the tests/e2e scripts (bare python3). Run it after staging the new file.
    when = function(names)
      return any(names, "^tests/e2e/")
    end,
    step = step("the e2e script census (tests/scripts, after staging the new e2e file)",
      gated(PYTEST .. " tests/scripts"), false),
  },
  {
    -- R62 (the vw594 fence gate): it mutates the real source by exact-string
    -- anchors in the files the source names; an edit that reshapes an anchored
    -- line reds develop with "non-vacuity: '<anchor>' in <source>". When an
    -- anchor moves, repoint the tuple and keep the mutation deleting the same
    -- stamp. The gate's other anchored files (the indexers, catalog/note_write.py)
    -- are not named by the source and are not triggers here.
    when = function(names)
      return any_of(names, FENCE_GATE_FILES)
    end,
    step = step("the vw594 fence coverage gate (an edit to an anchored line reds it; repoint the anchor)",
      gated(PYTEST .. " tests/test_vw594_fence_coverage_gate.py"), false),
  },
  {
    -- R7 (service substrate): the wire-ledger check scans COMMITS, so it runs
    -- after the commit; a lint bucket run on the uncommitted tree stays green and
    -- CI goes red (2026-09-16, 33ac72e02). Both halves in one diff is the
    -- source's trigger; the script decides per commit.
    when = function(names)
      return any(names, "^service/") and any(names, "^src/nexus/")
    end,
    step = step("the wire-contract pairing check: run it after the commit, before the push (it scans commits;"
      .. " a commit touching service/ and src/nexus/ together needs a docs/wire-contract-pending.md Unshipped"
      .. " bullet in the exact `-- engine tag `<tag>` -- [additive]` shape)",
      "uv run python scripts/check_wire_contract_pairing.py", false),
  },
  {
    -- R5 (service substrate; Recurred 2026-10-19, nexus-dxsjc; AGENTS.md rule 13):
    -- a push touching src/nexus/ or service/ is preceded by one full Python
    -- suite, once per tree. "Engine-only" in a brief is the defect. On chas in a
    -- lab-l grant through watched_pytest.py, the gate jar built first
    -- (docs/agents/test-running.md, Hand runs and develop CI, items 3 and 4).
    when = function(names)
      return any(names, "^src/nexus/") or any(names, "^service/")
    end,
    step = step("the full Python suite on this tree, once per tree: SKIP it when this tree (this sha, clean)"
      .. " already has a green full run, whoever ran it, or develop CI already has the verdict; announce it with"
      .. " its change class, the sha and 'no green full run of this tree exists' (AGENTS.md rule 13)",
      gated("lab-remote -c lab-l --fetch full-suite-status.json -- bash -c 'scripts/build-gate-jar.sh && "
        .. "python3 scripts/watched_pytest.py --stall 180 --status full-suite-status.json -- "
        .. PYTEST .. " -n auto'"),
      true),
  },
}

local function in_class(path, class)
  if class.except then
    for _, p in ipairs(class.except) do
      if string.find(path, p) then
        return false
      end
    end
  end
  for _, p in ipairs(class.patterns) do
    if string.find(path, p) then
      return true
    end
  end
  return false
end

local DOCS
for _, class in ipairs(CLASSES) do
  if class.name == "docs" then
    DOCS = class
  end
end

local names = git.diff_names(base)
local stat = git.diff_stat(base)

local members = {}
for _, class in ipairs(CLASSES) do
  members[class.name] = {}
end
local unclassified = {}
local code = false
for _, path in ipairs(names) do
  local found = false
  for _, class in ipairs(CLASSES) do
    if in_class(path, class) then
      table.insert(members[class.name], path)
      found = true
    end
  end
  if not found then
    table.insert(unclassified, path)
  end
  if not in_class(path, DOCS) then
    code = true
  end
end

local checks = {}
local function add(class, list)
  for _, s in ipairs(list) do
    s.class = class
    table.insert(checks, s)
  end
end

-- Order: the gate jar, then each class's own checks in table order, with the
-- standing checks after the python class's quick ones.
if code or #members.docs > 0 then
  add("code", {JAR})
end
for _, class in ipairs(CLASSES) do
  if #members[class.name] > 0 then
    add(class.name, class.checks(members[class.name]))
  end
  if class.name == "python" and code then
    add("code", standing(any(names, "^scripts/select_affected_integration_tests%.py$")
      or any(names, "^scripts/assert_integration_affected_ran%.py$")))
  end
end
for _, row in ipairs(TRIPWIRES) do
  if row.when(names) then
    add("tripwire", {row.step})
  end
end

local classes = {}
for _, class in ipairs(CLASSES) do
  if #members[class.name] > 0 then
    table.insert(classes, {name = class.name, files = members[class.name]})
  end
end

return {
  base = base,
  files = #names,
  added = stat.added,
  deleted = stat.deleted,
  empty = #names == 0,
  code = code,
  classes = classes,
  unclassified = unclassified,
  checks = checks,
}
