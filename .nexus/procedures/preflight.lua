-- procedure: preflight
-- summary: The checks a branch's diff since base needs, as commands to run; it runs none.
-- args: base:string
-- grants: git.diff_names, git.diff_stat

-- RDR-240 Technical Design 8 (nexus-qo7ie.32). Reads the diff from the merge
-- base of `base` and HEAD, names its change classes from the table below, and
-- returns the commands each class needs. It runs nothing: the sandbox has no
-- shell, and a heavy check runs through lab-remote at the caller's hand.
--
-- A new class is a row in CLASSES. A path belongs to every class one of whose
-- patterns matches it (Lua patterns, anchored with ^). "code" is CI's own
-- predicate: any path outside the docs class makes the push a code push
-- (.github/workflows/ci.yml, the doc-only fast lane), and a code push needs the
-- standing checks of the push train (T2 nexus/push-train rule 1).
--
-- The commands name every changed path, a deleted one included: the git verbs
-- do not say which paths were deleted, and the sandbox cannot look.

local args = ...
local base = args.base

local CLASSES = {
  {
    name = "python",
    patterns = {"%.py$", "^pyproject%.toml$", "^uv%.lock$"},
  },
  {
    name = "engine",
    patterns = {"^service/"},
  },
  {
    name = "schema",
    patterns = {"^service/src/main/resources/db/changelog/"},
  },
  {
    -- The plugin surface: the paths the drift-ledger workflow watches
    -- (.github/workflows/plugin-drift-ledger.yml, on.push.paths).
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
  },
  {
    name = "workflow",
    patterns = {"^%.github/workflows/[^/]+%.yml$", "^%.github/workflows/[^/]+%.yaml$"},
  },
  {
    -- CI's doc-only predicate, case by case; web/install.* is code there.
    name = "docs",
    patterns = {
      "^docs/", "^web/", "^conexus/CHANGELOG%.md$", "^README%.md$",
      "^CHANGELOG", "^LICENSE",
    },
    except = {"^web/install%."},
  },
}

-- A shell word: single-quoted, with each ' written '\''.
local function quote(s)
  if string.match(s, "^[%w%./_%-]+$") then
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

local function matches(path, patterns)
  for _, p in ipairs(patterns) do
    if string.find(path, p) then
      return true
    end
  end
  return false
end

local function in_class(path, class)
  if class.except and matches(path, class.except) then
    return false
  end
  return matches(path, class.patterns)
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
  local any = false
  for _, class in ipairs(CLASSES) do
    if in_class(path, class) then
      table.insert(members[class.name], path)
      any = true
    end
  end
  if not any then
    table.insert(unclassified, path)
  end
  if not in_class(path, DOCS) then
    code = true
  end
end

local checks = {}
local function check(class, what, command, heavy)
  table.insert(checks, {class = class, check = what, command = command, heavy = heavy})
end

local diff = "git diff --name-only " .. quote(base .. "...HEAD")

local py = members.python
if #py > 0 then
  local touched = {}
  for _, path in ipairs(py) do
    if string.find(path, "%.py$") then
      table.insert(touched, path)
    end
  end
  if #touched > 0 then
    check("python", "ruff check on the touched files", "uv run ruff check " .. words(touched), false)
    check("python", "ruff format on the touched files", "uv run ruff format --check " .. words(touched), false)
  end
  local tests = {}
  for _, path in ipairs(py) do
    if string.find(path, "^tests/.*test_[^/]*%.py$") then
      table.insert(tests, path)
    end
  end
  if #tests > 0 then
    check("python", "the changed test files", "uv run pytest -q -p no:randomly " .. words(tests), false)
  end
end

if code then
  check("code", "the whole lint bucket",
    "lab-remote -- uv run pytest -q -p no:randomly -m lint -n auto", true)
  check("code", "the drift ledger and the lifecycle gate (not lint-marked)",
    "uv run pytest -q -p no:randomly tests/test_plugin_release_drift_ledger.py tests/daemon/test_lifecycle_gate.py",
    false)
  check("code", "tests that mint a tenant, at +0 or below",
    "uv run scripts/tenant_delta.py --base " .. quote(base), false)
  check("code", "whether the change needs a native Windows run",
    "uv run scripts/windows_hand_test.py --check", false)
  check("code", "the integration tests the change affects",
    diff .. " | uv run python scripts/select_affected_integration_tests.py --repo .", false)
end

if #members.engine > 0 then
  check("engine", "rebuild the gate jar (the gate is mtime-keyed)", "scripts/build-gate-jar.sh", false)
  check("engine", "the engine main suite", "lab-remote -- scripts/mvnw-leased.sh -f service/pom.xml verify", true)
end

if #members.schema > 0 then
  check("schema", "the changeset validation lint",
    "uv run pytest -q -p no:randomly -m lint tests/test_changelog_validate_precondition_lint.py", false)
end

if #members.plugin > 0 then
  check("plugin", "the plugin release drift ledger",
    "uv run pytest -q -p no:randomly tests/test_plugin_release_drift_ledger.py", false)
end

if #members.workflow > 0 then
  check("workflow", "actionlint on the changed workflows", "actionlint " .. words(members.workflow), false)
end

if #members.docs > 0 and not code then
  check("docs", "the tests that read docs/ (a docs-only push skips the shard matrix)",
    "uv run pytest -q -p no:randomly $(uv run python scripts/select_docs_reader_tests.py --repo .)", false)
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
  code = code,
  classes = classes,
  unclassified = unclassified,
  checks = checks,
}
