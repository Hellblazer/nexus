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
        table.insert(out, step("the changed test files",
          live({":(glob)tests/**/test_*.py"}) .. " | xargs -0 -r uv run pytest -q -p no:randomly --", false))
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
    name = "schema",
    patterns = {"^service/src/main/resources/db/changelog/"},
    checks = function()
      return {
        step("the changeset validation lint",
          "uv run pytest -q -p no:randomly -m lint tests/test_changelog_validate_precondition_lint.py", false),
      }
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
    },
    checks = function(files)
      if any(files, "^conexus/hooks/") then
        return {step("the hook tests", "uv run pytest -q -p no:randomly tests/hooks", false)}
      end
      return {}
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
    -- CI's doc-only predicate, arm by arm: a path with a directory outside
    -- docs/ and web/ is code there, and so is web/install.*.
    name = "docs",
    patterns = {
      "^docs/", "^web/", "^conexus/CHANGELOG%.md$", "^README%.md$",
      "^CHANGELOG[^/]*$", "^LICENSE[^/]*$",
    },
    except = {"^web/install%."},
    checks = function()
      -- The selector exits 1 below its floor; the && stops there instead of
      -- letting an empty list run the whole suite. The list goes through
      -- xargs, which splits it in any shell (zsh does not split an unquoted
      -- $f).
      return {
        step("the tests that read docs/",
          "f=$(uv run python scripts/select_docs_reader_tests.py --repo .)"
            .. " && printf '%s\\n' \"$f\" | xargs uv run pytest -q -p no:randomly",
          false),
      }
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
    step("the whole lint bucket",
      "lab-remote -- bash -c 'scripts/build-gate-jar.sh && uv run pytest -q -p no:randomly -m lint -n auto'", true),
    step("the drift ledger and the lifecycle gate (not lint-marked)",
      "uv run pytest -q -p no:randomly tests/test_plugin_release_drift_ledger.py tests/daemon/test_lifecycle_gate.py",
      false),
    step("tests that mint a tenant, at +0 or below",
      "uv run scripts/tenant_delta.py --strict --base=" .. quote(base), false),
    step("whether the change needs a native Windows run (it reads origin/develop...HEAD)",
      "uv run scripts/windows_hand_test.py --check", false),
    -- CI's mark expression (ci.yml INTEGRATION_MARK_EXPR) and its rule that a
    -- change to the selector selects every integration file.
    step("the integration tests the change affects",
      "f=$(" .. sel .. ")"
        .. " && { [ -z \"$f\" ] || printf '%s\\n' \"$f\" | xargs lab-remote -- uv run pytest -q -p no:randomly"
        .. " -o addopts= -m " .. MARK .. "; }",
      true),
  }
end

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
