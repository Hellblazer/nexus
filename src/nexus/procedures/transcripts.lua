-- procedure: transcripts
-- summary: Count what Claude Code sessions did in a UTC window, by query: agents, tools, skills, and more.
-- args: query:string, since:string, until:string?, project:string?, now:string
-- grants: transcripts.sessions

-- RDR-240 Technical Design 5 (nexus-qo7ie.31). Seven queries over transcripts.sessions, one
-- UTC day per verb call, paged under the run's 1 MB result cap.
--
-- Arguments. query is one of the seven names. since is required. until is optional and
-- defaults to the reserved now; it is a Lua keyword, so the script reads it as
-- args["until"]. project is a Claude Code project directory name, default all.
--
-- Window. Half-open, [since, until), in UTC. A bare date is that day at 00:00 UTC; a
-- timestamp is YYYY-MM-DDTHH:MM:SS with an optional Z, always UTC; nothing else is read
-- (no offsets, no fractions). A session belongs to the window that holds its first record
-- (transcripts.sessions decides that), so a session spanning midnight is counted once, on
-- its start day. A session that starts exactly at since is in; one that starts exactly at
-- until is out.
--
-- Result: {rows, truncated_records, until, next_since}. until is the value the run used
-- (the until argument, else now). next_since is absent when the window is done; otherwise it is where the
-- next page starts: a date, or a timestamp when the page ended inside a day that was split
-- into hours. The caller runs again with since = next_since and the returned until, and joins the
-- pages with nexus.lua_runtime.transcripts_join.join_pages. A page adds whole days (hours
-- after a split) while its rows stay under PAGE_BYTES and, for the keyed queries, while its
-- sessions stay under MAX_SESSIONS_PER_RUN. A day whose verb result is too large, or whose
-- rows alone pass PAGE_BYTES, is split into hours; an hour that still does not fit fails
-- with an error starting result-window: that names the hour. A transcript over 1 MB after
-- the verb's cuts fails with the verb's record-too-large: error, unchanged.
--
-- Rows. A row's size is estimated as twice the byte length of its strings plus 16 bytes a
-- field (the sandbox has no JSON encoder). Per-session queries (memory, tools, loads)
-- return rows that pages concatenate. Keyed queries return rows summed by key; each field
-- of a keyed row is tagged for the join: sum adds, max takes the larger, mean is carried as
-- <field>_total and <field>_count (the join adds both and derives <field>_mean).
--   memory (per session): session_id, main, agent_id, kind, name, count, last_use, size.
--     kind is file_read or file_write (a Read, Write or Edit of a memory file), t2_get (a
--     memory_get call; name is project/title), t2_search (a memory_search call, which
--     carries no title; name is the project) or loaded (what loaded into a subagent's
--     instructions; size in bytes).
--   tools (per session): session_id, main, agent_id, kind, name, server, sub, deferred,
--     count, last_use, size. kind is tool (name is the tool; server is the MCP server of
--     an mcp__ tool; deferred is true when the session had the tool deferred; size is the
--     bytes of the call's string inputs), nx (a verb read from Bash command text: name is
--     "nx <verb>", sub the next word when it looks like a subcommand) or deferred_summary
--     (count is the tools deferred to the session, called how many of them were called).
--   loads (per session): session_id, main, agent_id, kind (the attachment), name (the
--     path), type, count, last_use, size (the largest size seen). main and agent_id tell a
--     main session from a dispatch.
--   agents (keyed kind, agent_type, model): dispatches sum, runs sum, turns sum, errors
--     sum, context mean, max_context max, cost_estimate_usd sum, unpriced sum. A dispatch
--     row is an Agent or Task call in a main session (the type and model it asked for; errors
--     is the call's own error flag); a run row is a subagent transcript (the model it ran,
--     its turns, its first-turn context, its tool errors). A subagent transcript names no
--     dispatch, so a run row's agent_type is (unattributed).
--   skills (keyed scope, kind, name): count, runs_with_skill, long_runs,
--     long_runs_without_skill, all sum. kind is skill, dispatch, slash or runs (one row per
--     scope with the run counts of scripts/skill_routing_census.py).
--   violations (keyed rule): count sum (main sessions with at least one hit), main_sessions
--     sum. Every page carries a row for every rule, zeros included, so no page drops its
--     denominator; the join derives per_100.
--   procedures (keyed procedure): runs sum, hand_written sum.
-- Cost. cost_estimate_usd is an ESTIMATE from the one price table below, USD per million
-- tokens, read from src/nexus/operators/llm_models.py PRICES (the published price list,
-- read through Context7 on 2026-10-23). A model not in the table adds to unpriced and
-- costs nothing here. The long-prompt tier of some models is not applied.
--
-- Violation rules (RDR-238 Phase 2; the five rules of its research 4, counted as main
-- sessions with at least one hit): em-dash in assistant prose (code spans and fences
-- left out); git-stash, a git stash command outside quotes and heredocs; full-disk-find,
-- a find whose start path is /, ~, $HOME, /Users, /home or one directory below the last
-- two; claude-session-trailer, a Claude-Session: line in a git commit command;
-- backtick-in-quoted-arg, a backtick inside a quoted argument of a command that runs nx or bd.

local args = ...

local PAGE_BYTES = 768 * 1024
local MAX_SESSIONS_PER_RUN = 1000
local LONG_RUN_TURNS = 50
local DAY = 86400
local HOUR = 3600
local FIELD_BYTES = 16

local function fail(message)
  error(message, 0)
end

local function str(value, default)
  if type(value) == "string" and value ~= "" then
    return value
  end
  return default
end

local function list(value)
  if type(value) == "table" then
    return value
  end
  return {}
end

local function input_of(call)
  if type(call.input) == "table" then
    return call.input
  end
  return {}
end

-- ---- time: no os, no clock; days since 1970-01-01 by the civil-calendar algorithm ----

local function days_from_civil(y, m, d)
  if m <= 2 then
    y = y - 1
  end
  local era = y // 400
  local yoe = y - era * 400
  local doy = (153 * ((m + 9) % 12) + 2) // 5 + d - 1
  local doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
  return era * 146097 + doe - 719468
end

local function civil_from_days(z)
  z = z + 719468
  local era = z // 146097
  local doe = z - era * 146097
  local yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
  local doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
  local mp = (5 * doy + 2) // 153
  local d = doy - (153 * mp + 2) // 5 + 1
  local m = mp + 3
  if mp >= 10 then
    m = mp - 9
  end
  local y = yoe + era * 400
  if m <= 2 then
    y = y + 1
  end
  return y, m, d
end

-- A date or a UTC timestamp as seconds since the epoch.
local function parse_time(text, name)
  local y, m, d = text:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
  local hh, mi, ss = 0, 0, 0
  if y == nil then
    y, m, d, hh, mi, ss = text:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)T(%d%d):(%d%d):(%d%d)Z?$")
  end
  if y == nil then
    fail(name .. " must be a UTC date (2026-11-07) or timestamp (2026-11-07T10:00:00Z); got " .. text)
  end
  y, m, d, hh, mi, ss = tonumber(y), tonumber(m), tonumber(d), tonumber(hh), tonumber(mi), tonumber(ss)
  local days = days_from_civil(y, m, d)
  local ry, rm, rd = civil_from_days(days)
  if ry ~= y or rm ~= m or rd ~= d or hh > 23 or mi > 59 or ss > 59 then
    fail(name .. " is not a real UTC time: " .. text)
  end
  return days * DAY + hh * HOUR + mi * 60 + ss
end

-- A midnight is written as a bare date, anything else as a timestamp.
local function stamp(t)
  local y, m, d = civil_from_days(t // DAY)
  local s = t % DAY
  if s == 0 then
    return string.format("%04d-%02d-%02d", y, m, d)
  end
  return string.format("%04d-%02d-%02dT%02d:%02d:%02dZ", y, m, d, s // HOUR, s % HOUR // 60, s % 60)
end

-- ---- rows ----

-- Twice the byte length of every string, plus 16 bytes a field.
local function estimate(rows)
  local n = 0
  for _, row in ipairs(rows) do
    for _, v in pairs(row) do
      n = n + FIELD_BYTES
      if type(v) == "string" then
        n = n + 2 * #v
      end
    end
  end
  return n
end

local function combine(into, row, tags)
  for field, tag in pairs(tags) do
    if tag == "mean" then
      into[field .. "_total"] = into[field .. "_total"] + (row[field .. "_total"] or 0)
      into[field .. "_count"] = into[field .. "_count"] + (row[field .. "_count"] or 0)
    elseif tag == "max" then
      into[field] = math.max(into[field], row[field] or 0)
    else
      into[field] = into[field] + (row[field] or 0)
    end
  end
end

-- Rows summed by key; keys are the row fields that identify it, tags how the others combine.
local function keyed(keys, tags)
  local acc = {index = {}, rows = {}}
  function acc.add(row)
    local parts = {}
    for i, k in ipairs(keys) do
      parts[i] = tostring(row[k])
    end
    local id = table.concat(parts, "\31")
    local into = acc.index[id]
    if into == nil then
      into = {}
      for _, k in ipairs(keys) do
        into[k] = row[k]
      end
      for field, tag in pairs(tags) do
        if tag == "mean" then
          into[field .. "_total"] = 0
          into[field .. "_count"] = 0
        else
          into[field] = 0
        end
      end
      acc.index[id] = into
      acc.rows[#acc.rows + 1] = {id = id, row = into}
    end
    combine(into, row, tags)
  end
  function acc.result()
    table.sort(acc.rows, function(a, b)
      return a.id < b.id
    end)
    local out = {}
    for i, entry in ipairs(acc.rows) do
      out[i] = entry.row
    end
    return out
  end
  return acc
end

-- One session's rows, grouped by an id, counted, with the last use and a size. mode is sum
-- (sizes add up) or max (the largest size seen).
local function tally(mode, rec)
  local t = {order = {}, by = {}}
  function t.hit(id, kind, name, time, size, extra)
    local r = t.by[id]
    if r == nil then
      r = {session_id = rec.session_id, main = rec.main == true, agent_id = rec.agent_id,
           kind = kind, name = name, count = 0, size = 0}
      if extra ~= nil then
        for k, v in pairs(extra) do
          r[k] = v
        end
      end
      t.by[id] = r
      t.order[#t.order + 1] = id
    end
    r.count = r.count + 1
    if type(time) == "string" and (r.last_use == nil or time > r.last_use) then
      r.last_use = time
    end
    if type(size) == "number" then
      if mode == "sum" then
        r.size = r.size + size
      elseif size > r.size then
        r.size = size
      end
    end
  end
  function t.emit(push)
    table.sort(t.order)
    for _, id in ipairs(t.order) do
      push(t.by[id])
    end
  end
  return t
end

-- ---- reading a command ----

local function words_of(cmd)
  local words = {}
  if type(cmd) == "string" then
    for w in cmd:gmatch("[^%s;&|()]+") do
      words[#words + 1] = w
    end
  end
  return words
end

local function is_word(w)
  return w ~= nil and w:match("^%a[%w%-]*$") ~= nil
end

-- Each nx call in a command as {verb, next word that looks like a subcommand or nil}.
local function nx_verbs(cmd)
  local found = {}
  local words = words_of(cmd)
  for i, w in ipairs(words) do
    if w == "nx" or w:match("/nx$") then
      local a, b = words[i + 1], words[i + 2]
      if is_word(a) then
        if not is_word(b) then
          b = nil
        end
        found[#found + 1] = {a, b}
      end
    end
  end
  return found
end

local function input_bytes(inp)
  local n = 0
  for _, v in pairs(inp) do
    if type(v) == "string" then
      n = n + #v
    end
  end
  return n
end

local function mcp_server(name)
  return name:match("^mcp__(.-)__")
end

local function tool_ends(name, tail)
  return name:match("^mcp__.*__" .. tail .. "$") ~= nil
end

-- ---- queries: each builder returns {keyed, bodies, keys, tags, each, finish} ----

local function build_tools()
  return {
    keyed = false,
    bodies = false,
    each = function(rec, push)
      local t = tally("sum", rec)
      local deferred, called, ncalled = {}, {}, 0
      local names = list(rec.deferred_tools)
      for _, n in ipairs(names) do
        deferred[n] = true
      end
      for _, c in ipairs(list(rec.tool_calls)) do
        local name = str(c.name, "(unnamed)")
        local inp = input_of(c)
        local server = mcp_server(name)
        local isdef = deferred[name] == true
        local extra = nil
        if server ~= nil or isdef then
          extra = {server = server, deferred = isdef or nil}
        end
        t.hit("t\31" .. name, "tool", name, c.time, input_bytes(inp), extra)
        if isdef and not called[name] then
          called[name] = true
          ncalled = ncalled + 1
        end
        if name == "Bash" then
          for _, v in ipairs(nx_verbs(inp.command)) do
            local sub = v[2]
            t.hit("n\31" .. v[1] .. "\31" .. tostring(sub), "nx", "nx " .. v[1], c.time, 0,
                  sub ~= nil and {sub = sub} or nil)
          end
        end
      end
      t.emit(push)
      if #names > 0 then
        push({session_id = rec.session_id, main = rec.main == true, agent_id = rec.agent_id,
              kind = "deferred_summary", name = "(deferred)", count = #names, called = ncalled, size = 0})
      end
    end,
  }
end

local function is_memory_path(p)
  return type(p) == "string" and (p:find("/memory/", 1, true) ~= nil or p:match("MEMORY%.md$") ~= nil)
end

local function build_memory()
  return {
    keyed = false,
    bodies = false,
    each = function(rec, push)
      local t = tally("max", rec)
      for _, c in ipairs(list(rec.tool_calls)) do
        local name = str(c.name, "")
        local inp = input_of(c)
        local path = inp.file_path
        if name == "Read" and is_memory_path(path) then
          t.hit("r\31" .. path, "file_read", path, c.time, nil)
        elseif (name == "Write" or name == "Edit" or name == "MultiEdit") and is_memory_path(path) then
          t.hit("w\31" .. path, "file_write", path, c.time, nil)
        elseif tool_ends(name, "memory_get") then
          local who = str(inp.project, "(none)") .. "/" .. str(inp.title, "")
          t.hit("g\31" .. who, "t2_get", who, c.time, nil)
        elseif tool_ends(name, "memory_search") then
          local project = str(inp.project, "(all)")
          t.hit("s\31" .. project, "t2_search", project, c.time, nil)
        end
      end
      if rec.main ~= true then
        for _, l in ipairs(list(rec.instruction_loads)) do
          if type(l.path) == "string" then
            t.hit("l\31" .. l.path, "loaded", l.path, l.time, l.size, {type = l.type})
          end
        end
      end
      t.emit(push)
    end,
  }
end

local function build_loads()
  return {
    keyed = false,
    bodies = false,
    each = function(rec, push)
      local t = tally("max", rec)
      for _, l in ipairs(list(rec.instruction_loads)) do
        if type(l.path) == "string" then
          local kind = str(l.kind, "(unknown)")
          t.hit(kind .. "\31" .. l.path .. "\31" .. tostring(l.type), kind, l.path, l.time, l.size,
                {type = l.type})
        end
      end
      t.emit(push)
    end,
  }
end

local function is_dispatch(name)
  return name == "Agent" or name == "Task"
end

-- Cost: USD per million tokens, {input, output, cache write, cache read}. ESTIMATE.
-- Table as of 2026-10-23, from src/nexus/operators/llm_models.py PRICES.
local function price_of(model)
  local prices = {
    ["claude-fable-5-1"] = {10.0, 50.0, 12.5, 0.25},
    ["claude-fable-5"] = {10.0, 50.0, 12.5, 1.0},
    ["claude-opus-5-5"] = {4.0, 20.0, 5.0, 0.20},
    ["claude-opus-5"] = {5.0, 25.0, 6.25, 0.50},
    ["claude-opus-4-8"] = {5.0, 25.0, 6.25, 0.50},
    ["claude-opus-4-7"] = {5.0, 25.0, 6.25, 0.50},
    ["claude-opus-4-6"] = {5.0, 25.0, 6.25, 0.50},
    ["claude-opus-4-5"] = {5.0, 25.0, 6.25, 0.50},
    ["claude-sonnet-5-5"] = {2.0, 10.0, 2.5, 0.10},
    ["claude-sonnet-5"] = {2.0, 10.0, 2.5, 0.20},
    ["claude-sonnet-4-6"] = {3.0, 15.0, 3.75, 0.30},
    ["claude-sonnet-4-5"] = {3.0, 15.0, 3.75, 0.30},
    ["claude-haiku-5-5"] = {0.10, 0.50, 0.125, 0.01},
    ["claude-haiku-4-5"] = {1.0, 5.0, 1.25, 0.10},
  }
  return prices[model] or prices[model:gsub("%-%d%d%d%d%d%d%d%d$", "")]
end

local function cost_of(model, tokens)
  local p = price_of(model)
  if p == nil or type(tokens) ~= "table" then
    return 0, false
  end
  local usd = ((tokens.input or 0) * p[1] + (tokens.output or 0) * p[2]
    + (tokens.cache_creation or 0) * p[3] + (tokens.cache_read or 0) * p[4]) / 1000000
  return usd, true
end

local function build_agents()
  return {
    keyed = true,
    bodies = false,
    keys = {"kind", "agent_type", "model"},
    tags = {dispatches = "sum", runs = "sum", turns = "sum", errors = "sum", context = "mean",
            max_context = "max", cost_estimate_usd = "sum", unpriced = "sum"},
    each = function(rec, add)
      if rec.main == true then
        for _, c in ipairs(list(rec.tool_calls)) do
          if is_dispatch(c.name) then
            local inp = input_of(c)
            add({kind = "dispatch", agent_type = str(inp.subagent_type, "(default)"),
                 model = str(inp.model, "(default)"), dispatches = 1, errors = c.is_error == true and 1 or 0})
          end
        end
        return
      end
      local errors = 0
      for _, c in ipairs(list(rec.tool_calls)) do
        if c.is_error == true then
          errors = errors + 1
        end
      end
      local model = str(list(rec.models)[1], "(unknown)")
      local ctx = rec.first_context_tokens
      local usd, priced = cost_of(model, rec.tokens)
      add({kind = "run", agent_type = "(unattributed)", model = model, runs = 1,
           turns = rec.assistant_turns or 0, errors = errors,
           context_total = ctx or 0, context_count = ctx and 1 or 0, max_context = ctx or 0,
           cost_estimate_usd = usd, unpriced = priced and 0 or 1})
    end,
  }
end

local function build_skills()
  return {
    keyed = true,
    bodies = false,
    keys = {"scope", "kind", "name"},
    tags = {count = "sum", runs_with_skill = "sum", long_runs = "sum", long_runs_without_skill = "sum"},
    each = function(rec, add)
      local scope = rec.main == true and "main" or "subagent"
      local with_skill = false
      for _, c in ipairs(list(rec.tool_calls)) do
        if c.name == "Skill" then
          with_skill = true
          add({scope = scope, kind = "skill", name = str(input_of(c).skill, "?"), count = 1})
        elseif is_dispatch(c.name) then
          add({scope = scope, kind = "dispatch", name = str(input_of(c).subagent_type, "(default)"), count = 1})
        end
      end
      for _, s in ipairs(list(rec.slash_commands)) do
        add({scope = scope, kind = "slash", name = str(s.name, "?"), count = 1})
      end
      local long = (rec.assistant_turns or 0) >= LONG_RUN_TURNS
      add({scope = scope, kind = "runs", name = "(all)", count = 1,
           runs_with_skill = with_skill and 1 or 0, long_runs = long and 1 or 0,
           long_runs_without_skill = (long and not with_skill) and 1 or 0})
    end,
  }
end

local RULES = {"em-dash", "git-stash", "full-disk-find", "claude-session-trailer", "backtick-in-quoted-arg"}

local function prose_has_em_dash(text)
  local dash = "\226\128\148"
  if type(text) ~= "string" or text:find(dash, 1, true) == nil then
    return false
  end
  local prose = text:gsub("```.-```", ""):gsub("`[^`\n]*`", "")
  return prose:find(dash, 1, true) ~= nil
end

-- The command up to its first heredoc, with quoted strings emptied.
local function outside_quotes(cmd)
  local head = cmd:match("^(.-)<<") or cmd
  return head:gsub('"[^"]*"', '""'):gsub("'[^']*'", "''")
end

local function whole_disk_find(cmd)
  local words = {}
  for w in cmd:gmatch("%S+") do
    words[#words + 1] = w
  end
  local roots = {["/"] = true, ["~"] = true, ["~/"] = true, ["$HOME"] = true, ["$HOME/"] = true,
                 ["${HOME}"] = true, ["/Users"] = true, ["/home"] = true}
  for i, w in ipairs(words) do
    if w == "find" then
      local j = i + 1
      while words[j] ~= nil and words[j]:sub(1, 1) == "-" do
        j = j + 1
      end
      local p = words[j]
      if p ~= nil and (roots[p] or p:match("^/Users/[^/]+/?$") or p:match("^/home/[^/]+/?$")) then
        return true
      end
    end
  end
  return false
end

local function quoted_backtick(cmd)
  local runs_cli = false
  for _, w in ipairs(words_of(cmd)) do
    if w == "nx" or w == "bd" then
      runs_cli = true
    end
  end
  return runs_cli and (cmd:find('"[^"]*`[^"]*"') ~= nil or cmd:find("'[^']*`[^']*'") ~= nil)
end

local function bash_hits(cmd, hit)
  local bare = outside_quotes(cmd)
  if bare:find("%f[%w]git%s+stash%f[%W]") then
    hit["git-stash"] = true
  end
  if whole_disk_find(bare) then
    hit["full-disk-find"] = true
  end
  if cmd:find("Claude%-Session:") and cmd:find("commit", 1, true) then
    hit["claude-session-trailer"] = true
  end
  if quoted_backtick(cmd) then
    hit["backtick-in-quoted-arg"] = true
  end
end

local function build_violations()
  return {
    keyed = true,
    bodies = true,
    keys = {"rule"},
    tags = {count = "sum", main_sessions = "sum"},
    each = function(rec, add)
      if rec.main ~= true then
        return
      end
      local hit = {}
      for _, t in ipairs(list(rec.assistant_text)) do
        if prose_has_em_dash(t.text) then
          hit["em-dash"] = true
        end
      end
      for _, c in ipairs(list(rec.tool_calls)) do
        local command = input_of(c).command
        if c.name == "Bash" and type(command) == "string" then
          bash_hits(command, hit)
        end
      end
      for _, rule in ipairs(RULES) do
        add({rule = rule, count = hit[rule] and 1 or 0, main_sessions = 1})
      end
    end,
    finish = function(add)
      for _, rule in ipairs(RULES) do
        add({rule = rule, count = 0, main_sessions = 0})
      end
    end,
  }
end

local function build_procedures()
  return {
    keyed = true,
    bodies = false,
    keys = {"procedure"},
    tags = {runs = "sum", hand_written = "sum"},
    each = function(rec, add)
      local runs, hand, put = {}, {}, {}
      local function bump(map, k)
        map[k] = (map[k] or 0) + 1
      end
      for _, c in ipairs(list(rec.tool_calls)) do
        local name = str(c.name, "")
        local inp = input_of(c)
        if tool_ends(name, "procedure_run") then
          bump(runs, str(inp.name, "(unnamed)"))
        elseif tool_ends(name, "memory_put") then
          if str(inp.tags, ""):find("kind:gate", 1, true) then
            bump(put, "gate_record")
          end
          if str(inp.title, ""):match("^%d+%-research%-%d+$") then
            bump(put, "research_add")
          end
        elseif name == "Bash" then
          local cmd = str(inp.command, "")
          local words = words_of(cmd)
          for i, w in ipairs(words) do
            local procedure = words[i + 2]
            if (w == "nx" or w:match("/nx$")) and words[i + 1] == "run" and procedure ~= nil
              and procedure:match("^[a-z][a-z0-9_]*$") then
              bump(runs, procedure)
            end
          end
          if cmd:find("nx memory put", 1, true) and cmd:find("kind:gate", 1, true) then
            bump(put, "gate_record")
          end
          if cmd:find("nx rdr research", 1, true) then
            bump(hand, "research_add")
          end
          if cmd:find("select_affected_integration_tests", 1, true)
            or cmd:find("select_docs_reader_tests", 1, true) then
            bump(hand, "preflight")
          end
          if cmd:find(".claude/projects", 1, true) and cmd:find("jsonl", 1, true) then
            bump(hand, "transcripts")
          end
        end
      end
      -- The caller writes what gate_record and research_add return with memory_put, so a
      -- tagged put that follows a run of the script is not a hand-written equivalent.
      for procedure, n in pairs(put) do
        hand[procedure] = (hand[procedure] or 0) + math.max(0, n - (runs[procedure] or 0))
      end
      for procedure, n in pairs(runs) do
        add({procedure = procedure, runs = n, hand_written = 0})
      end
      for procedure, n in pairs(hand) do
        add({procedure = procedure, runs = 0, hand_written = n})
      end
    end,
  }
end

local BUILDERS = {
  agents = build_agents,
  tools = build_tools,
  skills = build_skills,
  memory = build_memory,
  violations = build_violations,
  loads = build_loads,
  procedures = build_procedures,
}

-- ---- one unit of the window ----

local function reduce(q, recs)
  if q.keyed then
    local acc = keyed(q.keys, q.tags)
    for _, rec in ipairs(recs) do
      q.each(rec, acc.add)
    end
    if q.finish ~= nil then
      q.finish(acc.add)
    end
    return acc.result()
  end
  local rows = {}
  local function push(row)
    rows[#rows + 1] = row
  end
  for _, rec in ipairs(recs) do
    q.each(rec, push)
  end
  return rows
end

local function merge(q, held, more)
  if q.keyed then
    local acc = keyed(q.keys, q.tags)
    for _, row in ipairs(held) do
      acc.add(row)
    end
    for _, row in ipairs(more) do
      acc.add(row)
    end
    return acc.result()
  end
  local rows = {}
  for _, row in ipairs(held) do
    rows[#rows + 1] = row
  end
  for _, row in ipairs(more) do
    rows[#rows + 1] = row
  end
  return rows
end

-- The records of [a, b), or nil when the verb says the result is too large.
local function fetch(q, a, b)
  local ok, res = pcall(transcripts.sessions, stamp(a), stamp(b), args.project, q.bodies)
  if ok then
    return list(res)
  end
  if tostring(res):find("result-too-large:", 1, true) then
    return nil
  end
  error(res, 0)
end

-- Add the unit's rows to the page: ok, full (the page stops before this unit), or split (the
-- unit cannot be read, or its rows alone pass the page bound, and must be narrowed, wherever
-- the page stands).
local function admit(q, page, recs)
  if q.keyed and page.units > 0 and page.sessions + #recs > MAX_SESSIONS_PER_RUN then
    return "full"
  end
  local rows = reduce(q, recs)
  if estimate(rows) > PAGE_BYTES then
    return "split"
  end
  local merged = merge(q, page.rows, rows)
  if estimate(merged) > PAGE_BYTES then
    return "full"
  end
  local cut = 0
  for _, rec in ipairs(recs) do
    if rec.truncated == true then
      cut = cut + 1
    end
  end
  page.rows = merged
  page.units = page.units + 1
  page.sessions = page.sessions + #recs
  page.truncated = page.truncated + cut
  return "ok"
end

local function window(q, lo, hi)
  local page = {rows = {}, units = 0, sessions = 0, truncated = 0}
  local pos = lo
  local split_end = nil
  local next_since = nil
  while pos < hi do
    local stop
    if split_end ~= nil and pos < split_end then
      stop = math.min(split_end, (pos // HOUR + 1) * HOUR)
    else
      split_end = nil
      stop = math.min(hi, (pos // DAY + 1) * DAY)
    end
    local recs = fetch(q, pos, stop)
    local verdict = "split"
    if recs ~= nil then
      verdict = admit(q, page, recs)
    end
    if verdict == "ok" then
      pos = stop
    elseif verdict == "full" then
      next_since = stamp(pos)
      break
    elseif split_end ~= nil or stop - pos <= HOUR then
      fail("result-window: " .. stamp(pos) .. " to " .. stamp(stop)
        .. " is too large for one verb result or one page; narrow the window or the project")
    else
      split_end = stop
    end
  end
  return page, next_since
end

local build = BUILDERS[args.query]
if build == nil then
  fail("query must be one of agents, tools, skills, memory, violations, loads, procedures; got "
    .. tostring(args.query))
end

local until_text = args["until"] or args.now
local lo = parse_time(args.since, "since")
local hi = parse_time(until_text, "until")
local page, next_since = window(build(), lo, hi)

return {rows = page.rows, truncated_records = page.truncated, ["until"] = until_text, next_since = next_since}
