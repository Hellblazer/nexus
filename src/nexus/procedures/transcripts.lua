-- procedure: transcripts
-- summary: Count what Claude Code sessions did in a UTC window, by query: agents, tools, skills, and more.
-- args: query:string, since:string, until:string?, project:string?, now:string
-- grants: transcripts.sessions

-- RDR-240 Technical Design 5 (nexus-qo7ie.31). Seven queries over transcripts.sessions, one
-- UTC day per verb call, paged under the run's 1 MB result cap.
--
-- Arguments. query is one of the seven names. since is required and must be before until.
-- until defaults to the reserved now (a Lua keyword, so read as args["until"]). project is all
-- (the default, and what an absent project means), repo (every project directory of the
-- repository the run started in, its linked worktrees included) or one Claude Code project
-- directory name; the script passes it to the verb unchanged. Window: [since, until) in UTC. A
-- bare date is that day at 00:00 UTC; a timestamp is YYYY-MM-DDTHH:MM:SS with an optional Z;
-- nothing else is read. A session belongs to the window that holds its first record, so one
-- spanning midnight is counted once, on its start day.
--
-- Result: {rows, truncated_records, since, until, next_since}. since and until are the values
-- the run used. next_since is absent when the window is done; otherwise the next page starts
-- there (a date, or the hour a page ended at inside a day). Run again
-- with since = next_since and the returned until, and join the pages with
-- nexus.lua_runtime.transcripts_join.join_pages (it refuses a page whose since is not the
-- previous next_since). A page adds whole units (days, or hours) while its rows
-- stay under PAGE_BYTES, its verb reads (one a unit) under MAX_UNITS_PER_RUN (every query: a
-- year-long sparse window pages; each read decodes every transcript of its window), the tool
-- calls it reads under MAX_TOOL_CALLS_PER_RUN
-- (every query: the Lua instructions a page takes follow the commands it scans, about 1,300 a
-- call at worst, so 15,000 calls stay under 20 million of the runtime's 50 million) and, for
-- keyed queries, its sessions under MAX_SESSIONS_PER_RUN. A day that was read but does not
-- fit (the page is full, or the day's rows alone pass PAGE_BYTES, or its calls alone pass
-- MAX_TOOL_CALLS_PER_RUN) is divided by the hour each record starts in, from the records
-- already read: the page takes hours while they fit and ends at the first that does not, and
-- the next page reads that hour to midnight once (every query asks for start for this). Hour
-- reads remain for a day the verb refuses as too large, and for a day holding a start this
-- script cannot place in an hour (the verb takes any ISO offset, this reader UTC only). An hour
-- is taken whole, so a page always advances; an hour that still does not fit PAGE_BYTES or
-- the verb fails with result-window: naming the hour. A transcript
-- over 1 MB after the verb's cuts fails with the verb's record-too-large: error, unchanged.
--
-- Fields. Each query asks the verb only for the fields it reads (the fields argument of
-- transcripts.sessions): the verb then never keeps the rest, which cuts the size of its result
-- and the memory a page holds. A tool call's input rides only on the calls whose tool the query
-- reads (input@Bash, input@*__memory_get), so a query that reads no input of a tool never sees
-- it. Every query also asks for tool_call_count, the calls a record had before the filter, which
-- the page bound counts. Each unit is one read: the filter does not cut the read, every line is
-- still decoded, so a second read of a window doubles its time (tools, reading deferred_tools
-- apart, took 80 s over 30 days against 39 s in one read, whose busiest day was 10.3 MB).
-- tools measures a call's input size from the verb's input_bytes (the size before any cut), so
-- the input itself is not carried.
--
-- Rows. Size is estimated as twice the UTF-8 bytes of the strings plus 16 bytes a field (the
-- sandbox has no JSON encoder). Per-session queries return rows that pages concatenate; a
-- size no row measured is absent, not 0. Keyed queries return rows summed by key; each field
-- is tagged for the join: sum adds, max takes the larger, mean is carried as <field>_total
-- and <field>_count (the join derives <field>_mean).
--   memory (per session): session_id, main, agent_id, kind, name, count, last_use, size. kind
--     is file_read, file_write (a Read, Write or Edit of a memory file), t2_get (name is
--     project/title), t2_search (name is the project) or loaded (what loaded into a
--     subagent's instructions; size in bytes). last_use compares parsed times.
--   tools (per session): session_id, main, agent_id, kind, name, server, sub, deferred, count,
--     last_use, size. kind is tool (server of an mcp__ tool; deferred when the session had it
--     deferred; size the bytes of the call's string inputs), nx (name "nx <verb>"; see below)
--     or deferred_summary (count tools deferred to the session, called how many were called).
--   loads (per session): session_id, main, agent_id, kind (the attachment), name (the path),
--     type, count, last_use, size (the largest seen).
--   agents (keyed kind, agent_type, model): dispatches, runs, turns, errors, cost_estimate_usd,
--     unpriced sum; context mean; max_context max. A dispatch row is an Agent or Task call in
--     a main session (type and model it asked for). A run row is a subagent transcript (its
--     turns, first-turn context, tool errors). A subagent record shares its parent's
--     session_id: within a unit, each session's subagents in start order take that session's
--     Agent and Task calls in time order, each the first call not yet taken at or before its
--     start, and the run row gets that call's agent_type and, if it named one, its model (cost
--     is always priced by the model the run used). Unmatched runs are (unattributed); a
--     parent and child in different units do not match. The match pairs by session and start
--     order only: a dispatch that failed before it started a subagent can shift the later
--     matches of its session by one (is_error also marks a subagent that ran and failed, so a
--     failed call cannot be skipped). Per-type counts of runs are exact; the type a run is
--     named for is a best effort.
--   skills (keyed scope, kind, name): count, runs_with_skill, long_runs,
--     long_runs_without_skill sum. kind is skill, dispatch, slash or runs (one row per scope
--     with the run counts of scripts/skill_routing_census.py). LONG_RUN_TURNS counts distinct
--     API messages (assistant_turns), about 1.6 times fewer than the census's assistant
--     records, so the census's 50 records is scaled to 31 messages.
--   violations (keyed rule): count (main sessions with a hit), main_sessions sum. Every page
--     carries every rule, zeros included, so no page drops its denominator.
--   procedures (keyed procedure): runs, hand_written sum.
-- A Bash command is read by segment (split at ; & | ( ) newlines): quotes pair as the shell
-- pairs them, comments and heredoc bodies are never a command. nx verbs: nx is a segment's
-- command word, the verb the bare lowercase word after it, sub the next such word (so for
-- nx search foo, sub is foo; a quoted word, a path or a flag never is). The command word is
-- found past env, time, sudo, nohup, uv run, NAME=value and these wrappers with their options:
-- timeout (its signal and kill options, then its duration), nice (-n N, -N), watch (-n N, -d)
-- and xargs (-n N, -I str, -P N and the rest of its flags). Known limits: an odd number of
-- double quotes inside a heredoc message can hide a Claude-Session trailer; 2>&1 in the middle
-- of a command splits its segment (the & is a separator); $(...) nested inside double quotes is
-- not scanned; the bodies of bash -c '...' and ssh host '...' are one quoted word and are not
-- scanned.
-- Counts are floors where the verb cut a record to 1 MB: a cut record loses the tails of its
-- assistant text and tool calls, so the counts are biased low on exactly the long sessions;
-- truncated_records says how many records were cut. Project repo finds a repository's project
-- directories by the working directory each session recorded, and fails when the run started
-- outside a repository; a project directory literally named repo cannot be asked for by name.
-- Cost. cost_estimate_usd is an ESTIMATE from the price table below, USD per million tokens,
-- from src/nexus/operators/llm_models.py PRICES (read through Context7 on 2026-10-23; a test
-- compares them). A model not in it adds to unpriced and costs nothing. Long-prompt tiers
-- are not applied.
--
-- Violation rules (RDR-238 Phase 2): em-dash in assistant prose (code left out); git-stash,
-- a git command whose subcommand, past the global options (-C dir, --no-pager), is stash;
-- full-disk-find, a find whose start path (quoted or not) is /, ~, $HOME, /Users, /home or
-- one directory below the last two; claude-session-trailer, a Claude-Session: line in a git
-- commit segment (its arguments or its heredoc); backtick-in-quoted-arg, a backtick inside
-- double quotes in a segment whose command is nx or bd (inside single quotes it is literal).

local args = ...

local PAGE_BYTES = 768 * 1024
local MAX_SESSIONS_PER_RUN = 1000
local LONG_RUN_TURNS = 31
local MAX_UNITS_PER_RUN = 100
local MAX_TOOL_CALLS_PER_RUN = 15000
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

-- A transcript timestamp (a fraction allowed) as seconds, or nil when it is not UTC ISO.
local function ts_value(text)
  local y, m, d, hh, mi, ss, frac =
    tostring(text):match("^(%d%d%d%d)%-(%d%d)%-(%d%d)T(%d%d):(%d%d):(%d%d)(%.?%d*)Z?$")
  if y == nil then
    return nil
  end
  return days_from_civil(tonumber(y), tonumber(m), tonumber(d)) * DAY + tonumber(hh) * HOUR
    + tonumber(mi) * 60 + tonumber(ss) + (tonumber("0" .. frac) or 0)
end

-- Is time a after time b: by parsed time, else (a form that is not UTC ISO) by text.
local function later(a, b)
  local x, y = ts_value(a), ts_value(b)
  if x ~= nil and y ~= nil then
    return x > y
  end
  return a > b
end

-- ---- rows ----

-- Twice the byte length of every string, plus 16 bytes a field.
local function row_bytes(row)
  local n = 0
  for _, v in pairs(row) do
    n = n + FIELD_BYTES
    if type(v) == "string" then
      n = n + 2 * #v
    end
  end
  return n
end

local function estimate(rows)
  local n = 0
  for _, row in ipairs(rows) do
    n = n + row_bytes(row)
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
  local function id_of(row)
    local parts = {}
    for i, k in ipairs(keys) do
      parts[i] = tostring(row[k])
    end
    return table.concat(parts, "\31")
  end
  function acc.has(row)
    return acc.index[id_of(row)] ~= nil
  end
  function acc.add(row)
    local id = id_of(row)
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
           kind = kind, name = name, count = 0}
      if extra ~= nil then
        for k, v in pairs(extra) do
          r[k] = v
        end
      end
      t.by[id] = r
      t.order[#t.order + 1] = id
    end
    r.count = r.count + 1
    if type(time) == "string" and (r.last_use == nil or later(time, r.last_use)) then
      r.last_use = time
    end
    if type(size) == "number" then
      if r.size == nil then
        r.size = size
      elseif mode == "sum" then
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

-- A Bash command split into segments at ; & | ( ) newlines and backticks. A segment is
-- {w = words with their quotes removed, b = whether each word was wholly unquoted, bt = a
-- backtick inside double quotes, docs = the heredoc bodies it opened}. Quotes pair as the
-- shell pairs them, a comment (# at a word start) is dropped, a heredoc body belongs to its
-- segment's docs and nowhere else. Jumps over runs with find, so a long command stays cheap.
local function scan(cmd)
  local segs, seg, pending = {}, nil, {}
  local buf, started, bare = {}, false, true
  local n, i = #cmd, 1
  local function newseg()
    seg = {w = {}, b = {}, bt = false, docs = {}}
    segs[#segs + 1] = seg
  end
  local function endword()
    if started then
      seg.w[#seg.w + 1] = table.concat(buf)
      seg.b[#seg.b + 1] = bare
    end
    buf, started, bare = {}, false, true
  end
  local function add(text)
    buf[#buf + 1] = text
    started = true
  end
  local function bodies(from)
    for _, h in ipairs(pending) do
      local p, stop, after = from, nil, nil
      while p <= n do
        local e = cmd:find("\n", p, true) or n + 1
        if (cmd:sub(p, e - 1):gsub("^\t+", "")) == h.delim then
          stop, after = p - 1, e + 1
          break
        end
        p = e + 1
      end
      h.seg.docs[#h.seg.docs + 1] = cmd:sub(from, stop or n)
      from = after or n + 1
    end
    pending = {}
    return from
  end
  newseg()
  while i <= n do
    local j = cmd:find("[%s;&|()`'\"\\#<]", i)
    if j == nil then
      add(cmd:sub(i))
      break
    end
    if j > i then
      add(cmd:sub(i, j - 1))
    end
    local c = cmd:sub(j, j)
    i = j + 1
    if c == "\n" then
      endword()
      newseg()
      i = bodies(i)
    elseif c:match("%s") then
      endword()
    elseif c == "#" then
      if started then
        add("#")
      else
        i = cmd:find("\n", i, true) or n + 1
      end
    elseif c == ";" or c == "&" or c == "|" or c == "(" or c == ")" or c == "`" then
      endword()
      newseg()
    elseif c == "\\" then
      local nxt = cmd:sub(i, i)
      i = i + 1
      if nxt ~= "\n" and nxt ~= "" then
        add(nxt)
        bare = false
      end
    elseif c == "'" then
      local e = cmd:find("'", i, true) or n + 1
      add(cmd:sub(i, e - 1))
      bare = false
      i = e + 1
    elseif c == '"' then
      add("")
      bare = false
      while i <= n do
        local k = cmd:find('["\\`]', i)
        if k == nil then
          add(cmd:sub(i))
          i = n + 1
          break
        end
        add(cmd:sub(i, k - 1))
        local d = cmd:sub(k, k)
        i = k + 1
        if d == '"' then
          break
        elseif d == "\\" then
          local x = cmd:sub(i, i)
          if x == '"' or x == "\\" or x == "$" or x == "`" then
            add(x)
          elseif x ~= "\n" then
            add("\\" .. x)
          end
          i = i + 1
        else
          seg.bt = true
          add("`")
        end
      end
    elseif cmd:sub(i, i + 1) == "<<" then
      add("<<<")
      i = i + 2
    elseif cmd:sub(i, i) == "<" then
      local k = i + 1
      if cmd:sub(k, k) == "-" then
        k = k + 1
      end
      local _, sp = cmd:find("^[ \t]*", k)
      k = sp + 1
      local s, e, q, delim = cmd:find("^(['\"])(.-)%1", k)
      if s == nil then
        s, e, delim = cmd:find("^\\?([%w_.%-]+)", k)
      end
      if s ~= nil then
        endword()
        pending[#pending + 1] = {delim = delim, seg = seg}
        i = e + 1
      else
        add("<<")
        i = i + 1
      end
    else
      add("<")
    end
  end
  endword()
  return segs
end

-- Words a segment may start with that are not its command.
local SKIP = {env = true, time = true, sudo = true, command = true, exec = true, nohup = true,
              uv = true, run = true, ["{"] = true, ["!"] = true, ["then"] = true, ["do"] = true,
              ["else"] = true}

-- Wrappers that run the real command after their own options: the options that take a value
-- (any other word starting with - is one word), and whether a duration comes first.
local WRAPPERS = {
  timeout = {{["-s"] = true, ["-k"] = true, ["--signal"] = true, ["--kill-after"] = true}, true},
  nice = {{["-n"] = true, ["--adjustment"] = true}, false},
  watch = {{["-n"] = true, ["--interval"] = true}, false},
  xargs = {{["-n"] = true, ["-I"] = true, ["-P"] = true, ["-L"] = true, ["-d"] = true, ["-s"] = true,
            ["-a"] = true, ["-E"] = true, ["--max-args"] = true, ["--max-procs"] = true,
            ["--delimiter"] = true, ["--arg-file"] = true, ["--max-lines"] = true}, false},
}

-- The index of the word after a wrapper at k and its options (and a duration).
local function past_wrapper(seg, k, wrapper)
  local j = k + 1
  while seg.w[j] ~= nil and #seg.w[j] > 1 and seg.w[j]:sub(1, 1) == "-" do
    j = j + (wrapper[1][seg.w[j]] and 2 or 1)
  end
  if wrapper[2] and seg.w[j] ~= nil and seg.w[j]:match("^[%d.]+[smhd]?$") then
    j = j + 1
  end
  return j
end

-- The index of a segment's command word, or nil.
local function head(seg)
  local k = 1
  while seg.w[k] ~= nil do
    local w = seg.w[k]
    local wrapper = WRAPPERS[w:match("[^/]*$")]
    if SKIP[w] or w:match("^[%a_][%w_]*=") then
      k = k + 1
    elseif wrapper ~= nil then
      k = past_wrapper(seg, k, wrapper)
    else
      return k
    end
  end
  return nil
end

local function is_cmd(w, name)
  return w == name or w:sub(-#name - 1) == "/" .. name
end

-- Does one of the segment's words contain the text.
local function names(seg, text)
  for _, w in ipairs(seg.w) do
    if w:find(text, 1, true) then
      return true
    end
  end
  return false
end

local GIT_ARG = {["-C"] = true, ["-c"] = true, ["--git-dir"] = true, ["--work-tree"] = true,
                 ["--namespace"] = true, ["--super-prefix"] = true, ["--config-env"] = true}

-- The git subcommand of a segment whose command word is at k, past the global options.
local function git_sub(seg, k)
  local j = k + 1
  while seg.w[j] ~= nil do
    local w = seg.w[j]
    if GIT_ARG[w] then
      j = j + 2
    elseif w:sub(1, 1) == "-" then
      j = j + 1
    else
      return w
    end
  end
  return nil
end

local VERB = "^[a-z][a-z0-9_%-]*$"

-- Each nx call as {verb, sub}: nx is the command word of a segment, the verb the bare
-- lowercase word after it, and sub the next word when that is one too (so for
-- nx search foo, sub is foo; a quoted word, a path or a flag never is).
local function nx_verbs(segs)
  local found = {}
  for _, seg in ipairs(segs) do
    local k = head(seg)
    if k ~= nil and is_cmd(seg.w[k], "nx") and seg.b[k + 1] and seg.w[k + 1]:match(VERB) then
      local b = seg.w[k + 2]
      found[#found + 1] = {seg.w[k + 1], (seg.b[k + 2] and b:match(VERB)) and b or nil}
    end
  end
  return found
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
    fields = "tool_call_count,start,tool_calls.name,tool_calls.time,tool_calls.input_bytes," ..
      "tool_calls.input@Bash,deferred_tools",
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
        local size = type(c.input_bytes) == "number" and c.input_bytes or 0
        t.hit("t\31" .. name, "tool", name, c.time, size, extra)
        if isdef and not called[name] then
          called[name] = true
          ncalled = ncalled + 1
        end
        if name == "Bash" and type(inp.command) == "string" then
          for _, v in ipairs(nx_verbs(scan(inp.command))) do
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
    fields = "tool_call_count,start,instruction_loads,tool_calls.name,tool_calls.time," ..
      "tool_calls.input@Read,tool_calls.input@Write,tool_calls.input@Edit,tool_calls.input@MultiEdit," ..
      "tool_calls.input@*__memory_get,tool_calls.input@*__memory_search",
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
    fields = "tool_call_count,start,instruction_loads",
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
  local attributed = {}
  return {
    keyed = true,
    bodies = false,
    fields = "tool_call_count,start,models,tokens,first_context_tokens,assistant_turns," ..
      "tool_calls.name,tool_calls.time,tool_calls.is_error,tool_calls.input@Agent,tool_calls.input@Task",
    keys = {"kind", "agent_type", "model"},
    tags = {dispatches = "sum", runs = "sum", turns = "sum", errors = "sum", context = "mean",
            max_context = "max", cost_estimate_usd = "sum", unpriced = "sum"},
    -- A subagent record shares its parent's session_id. Within a unit, each session's
    -- subagents in start order take that session's Agent and Task calls in time order, each
    -- the first call not yet taken that is at or before its start; the rest stay unattributed.
    begin = function(recs)
      attributed = {}
      local calls, subs = {}, {}
      for _, rec in ipairs(recs) do
        local sid = tostring(rec.session_id)
        if rec.main == true then
          for _, c in ipairs(list(rec.tool_calls)) do
            local at = is_dispatch(c.name) and ts_value(c.time) or nil
            if at ~= nil then
              local inp = input_of(c)
              local l = calls[sid] or {}
              calls[sid] = l
              l[#l + 1] = {at = at, n = #l, type = str(inp.subagent_type, "(default)"), model = str(inp.model, nil)}
            end
          end
        else
          local at = ts_value(rec.start)
          if at ~= nil then
            local l = subs[sid] or {}
            subs[sid] = l
            l[#l + 1] = {at = at, id = tostring(rec.agent_id), rec = rec}
          end
        end
      end
      for sid, l in pairs(subs) do
        local d = calls[sid]
        if d ~= nil then
          table.sort(d, function(a, b)
            return a.at < b.at or (a.at == b.at and a.n < b.n)
          end)
          table.sort(l, function(a, b)
            return a.at < b.at or (a.at == b.at and a.id < b.id)
          end)
          local p = 1
          for _, s in ipairs(l) do
            if d[p] ~= nil and d[p].at <= s.at then
              attributed[s.rec] = d[p]
              p = p + 1
            end
          end
        end
      end
    end,
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
      local from = attributed[rec]
      add({kind = "run", agent_type = from and from.type or "(unattributed)",
           model = from and from.model or model, runs = 1,
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
    fields = "tool_call_count,start,assistant_turns,slash_commands,tool_calls.name,tool_calls.input@Skill," ..
      "tool_calls.input@Agent,tool_calls.input@Task",
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

local ROOTS = {["/"] = true, ["~"] = true, ["~/"] = true, ["$HOME"] = true, ["$HOME/"] = true,
               ["${HOME}"] = true, ["${HOME}/"] = true, ["/Users"] = true, ["/Users/"] = true,
               ["/home"] = true, ["/home/"] = true}

-- Is the start path of the find at k a whole-disk root (quoted or not).
local function disk_root(seg, k)
  local j = k + 1
  while seg.w[j] ~= nil and seg.w[j]:sub(1, 1) == "-" do
    j = j + 1
  end
  local p = seg.w[j]
  return p ~= nil and (ROOTS[p] == true or p:match("^/Users/[^/]+/?$") ~= nil
    or p:match("^/home/[^/]+/?$") ~= nil)
end

local function names_trailer(seg)
  for _, w in ipairs(seg.w) do
    if w:find("Claude-Session:", 1, true) then
      return true
    end
  end
  for _, d in ipairs(seg.docs) do
    if d:find("Claude-Session:", 1, true) then
      return true
    end
  end
  return false
end

-- The command word of each segment decides: git (its subcommand past the global options),
-- find, nx or bd. Quoted text, comments and heredoc bodies are never a command.
local function bash_hits(cmd, hit)
  for _, seg in ipairs(scan(cmd)) do
    local k = head(seg)
    local c = k and seg.w[k] or ""
    if is_cmd(c, "git") then
      local sub = git_sub(seg, k)
      if sub == "stash" then
        hit["git-stash"] = true
      elseif sub == "commit" and names_trailer(seg) then
        hit["claude-session-trailer"] = true
      end
    elseif is_cmd(c, "find") then
      if disk_root(seg, k) then
        hit["full-disk-find"] = true
      end
    elseif (is_cmd(c, "nx") or is_cmd(c, "bd")) and seg.bt then
      hit["backtick-in-quoted-arg"] = true
    end
  end
end

local function build_violations()
  return {
    keyed = true,
    bodies = true,
    fields = "tool_call_count,start,assistant_text,tool_calls.name,tool_calls.input@Bash",
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
    fields = "tool_call_count,start,tool_calls.name,tool_calls.input@*__procedure_run," ..
      "tool_calls.input@*__memory_put,tool_calls.input@Bash",
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
          -- By segment, as the nx verbs are: a command word decides, so a quoted word, an echo,
          -- a comment or a heredoc body is never a hand-written equivalent.
          local segs = scan(str(inp.command, ""))
          for _, v in ipairs(nx_verbs(segs)) do
            if v[1] == "run" and v[2] ~= nil and v[2]:match("^[a-z][a-z0-9_]*$") then
              bump(runs, v[2])
            end
          end
          for _, seg in ipairs(segs) do
            local k = head(seg)
            local c = k and seg.w[k] or ""
            if is_cmd(c, "nx") and seg.w[k + 1] == "memory" and seg.w[k + 2] == "put"
              and names(seg, "kind:gate") then
              bump(put, "gate_record")
            elseif is_cmd(c, "nx") and seg.w[k + 1] == "rdr" and seg.w[k + 2] == "research" then
              bump(hand, "research_add")
            elseif k ~= nil and c ~= "echo" and c ~= "printf" then
              if names(seg, "select_affected_integration_tests") or names(seg, "select_docs_reader_tests") then
                bump(hand, "preflight")
              end
              if names(seg, ".claude/projects") and names(seg, "jsonl") then
                bump(hand, "transcripts")
              end
            end
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
  if q.begin ~= nil then
    q.begin(recs)
  end
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

-- One verb read of [a, b), counted against the page's reads: the records, or nil when the verb
-- says the result is too large.
local function read(page, a, b, bodies, fields)
  page.calls = page.calls + 1
  local ok, res = pcall(transcripts.sessions, stamp(a), stamp(b), args.project, bodies, fields)
  if ok then
    return list(res)
  end
  if tostring(res):find("result-too-large:", 1, true) then
    return nil
  end
  error(res, 0)
end

-- The records of [a, b) with the fields the query asks for, or nil when the verb says the read
-- is too large.
local function fetch(q, page, a, b)
  return read(page, a, b, q.bodies, q.fields)
end

-- Add the unit's rows to the page: ok, full (the page stops before this unit), or split (the
-- unit cannot be read, or its rows alone pass the page bound, and must be narrowed, wherever
-- the page stands). The page keeps a running byte estimate: a per-session query appends the
-- unit's rows, a keyed one sums them into the page's accumulator, and a row that is not yet
-- there is the only growth, so no unit rescans the page.
local function admit(q, page, recs, narrow)
  local calls = 0
  for _, rec in ipairs(recs) do
    local n = rec.tool_call_count
    if type(n) ~= "number" then
      n = #list(rec.tool_calls)
    end
    calls = calls + n
  end
  if calls > 0 and page.units > 0 and page.tool_calls + calls > MAX_TOOL_CALLS_PER_RUN then
    return "full"
  end
  if narrow and calls > MAX_TOOL_CALLS_PER_RUN then
    return "split"
  end
  if q.keyed and page.units > 0 and page.sessions + #recs > MAX_SESSIONS_PER_RUN then
    return "full"
  end
  local rows = reduce(q, recs)
  local size = estimate(rows)
  if size > PAGE_BYTES then
    return "split"
  end
  local grown = size
  if q.keyed then
    grown = 0
    for _, row in ipairs(rows) do
      if not page.acc.has(row) then
        grown = grown + row_bytes(row)
      end
    end
  end
  if page.bytes + grown > PAGE_BYTES then
    return "full"
  end
  for _, row in ipairs(rows) do
    if q.keyed then
      page.acc.add(row)
    else
      page.rows[#page.rows + 1] = row
    end
  end
  for _, rec in ipairs(recs) do
    if rec.truncated == true then
      page.truncated = page.truncated + 1
    end
  end
  page.bytes = page.bytes + grown
  page.units = page.units + 1
  page.sessions = page.sessions + #recs
  page.tool_calls = page.tool_calls + calls
  return "ok"
end

-- The records of a unit grouped by the hour their transcript starts in, in order: {hour, recs};
-- nil when a record's start does not parse here (the verb takes any ISO offset, this reader only
-- UTC) or lies before the unit, since a record put in the wrong hour would be read again by the
-- next page. The caller then narrows by hour reads, as for a unit the verb refuses.
local function by_hour(recs, from)
  local groups, at = {}, {}
  for _, rec in ipairs(recs) do
    local t = ts_value(rec.start)
    if t == nil or t < from then
      return nil
    end
    local h = math.floor(t) // HOUR * HOUR
    local g = at[h]
    if g == nil then
      g = {hour = h, recs = {}}
      at[h] = g
      groups[#groups + 1] = g
    end
    g.recs[#g.recs + 1] = rec
  end
  table.sort(groups, function(a, b) return a.hour < b.hour end)
  return groups
end

local function window(q, lo, hi)
  local page = {rows = {}, bytes = 0, units = 0, calls = 0, sessions = 0, tool_calls = 0, truncated = 0}
  if q.keyed then
    page.acc = keyed(q.keys, q.tags)
  end
  local pos = lo
  local split_end = nil
  local next_since = nil
  while pos < hi do
    if page.units > 0 and page.calls >= MAX_UNITS_PER_RUN then
      next_since = stamp(pos)
      break
    end
    local stop
    if split_end ~= nil and pos < split_end then
      stop = math.min(split_end, (pos // HOUR + 1) * HOUR)
    else
      split_end = nil
      stop = math.min(hi, (pos // DAY + 1) * DAY)
    end
    local recs = fetch(q, page, pos, stop)
    local verdict = "split"
    if recs ~= nil then
      verdict = admit(q, page, recs, stop - pos > HOUR)
    end
    local groups = nil
    if verdict ~= "ok" and recs ~= nil and stop - pos > HOUR then
      groups = by_hour(recs, pos)
    end
    if groups ~= nil then
      -- The unit was read but does not fit: admit its hours from the records in hand, so the
      -- page ends at an hour and the next page reads only the rest of the day, once. An hour is
      -- never divided: one over the call bound is taken whole on an empty page, as an hour read is.
      -- The page ends where what it admitted ends: at the unit's start when no hour fit.
      for i, g in ipairs(groups) do
        verdict = admit(q, page, g.recs, false)
        if verdict ~= "ok" then
          if i > 1 or verdict ~= "full" then
            pos = math.max(pos, g.hour)
          end
          break
        end
      end
      if verdict == "ok" then
        pos = stop
      elseif verdict == "full" then
        next_since = stamp(pos)
        break
      else
        fail("result-window: " .. stamp(pos) .. " to " .. stamp(math.min(stop, (pos // HOUR + 1) * HOUR))
          .. " is too large for one verb result or one page; narrow the window or the project")
      end
    elseif verdict == "ok" then
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
  if q.keyed then
    page.rows = page.acc.result()
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
if lo >= hi then
  fail("since must be before until; got " .. args.since .. " and " .. until_text)
end
local page, next_since = window(build(), lo, hi)

return {rows = page.rows, truncated_records = page.truncated, since = args.since,
        ["until"] = until_text, next_since = next_since}
