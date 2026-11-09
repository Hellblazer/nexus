-- procedure: gate_record
-- summary: Build a review agent's T2 gate record; the caller writes it with memory_put.
-- args: kind:string, bead:string, rdr:integer?, sha:string, outcome:string, body:string, round:integer?, now:string, repo:string
-- grants: none

-- RDR-240 Technical Design 6 (nexus-qo7ie.34). The review agents' gate records in T2
-- project <repo>, the ones the RDR-209 rollup reads by their kind:gate, bead: and rdr:
-- tags. Not the RDR gate flow's records in <repo>_rdr, which keep their titles.
-- Returns {project, title, tags, content}; the caller writes the four fields unchanged
-- with memory_put and no TTL. Nothing here writes, so a caller that edits the tags is
-- not stopped; the lint over the agents' write-back lines and the rollup's own tag check
-- catch it.
--
-- The title is <kind>-<sha>-<bead>-<date>[-r<round>]: the slug Technical Design 6 leaves
-- open is the bead id (RDR-240 gate r2 Significant 6), so two records about one commit for
-- two beads never share a title, and the optional round separates a re-dispatched pass.
-- Two passes of one kind with the same sha, bead, day and round share a title, and the
-- second memory_put replaces the first. Every kind leads the title, where the audit-tag
-- table matches it.

local args = ...

local KINDS = {"review", "critique", "confirm", "fix-check"}
-- The verdict vocabulary the review agents mandate (their `- **outcome**:` line).
local OUTCOMES = {"justified", "partial", "not-justified"}

local function one_of(name, value, allowed)
  for _, v in ipairs(allowed) do
    if v == value then
      return
    end
  end
  error(name .. " must be one of " .. table.concat(allowed, ", ") .. "; got " .. value, 0)
end

one_of("kind", args.kind, KINDS)
one_of("outcome", args.outcome, OUTCOMES)

local sha = args.sha
if not string.match(sha, "^[0-9a-f]+$") or #sha < 7 or #sha > 64 then
  error("sha must be 7 to 64 lower-case hex digits; got " .. sha, 0)
end

local bead = args.bead
-- Any repo's bead id (nexus-qo7ie.34, ART-12, my-proj-abc12): one token with no space or
-- comma, since the id goes into a comma-separated tag list and the title.
if not string.match(bead, "^[A-Za-z0-9][A-Za-z0-9._-]*$") then
  error("bead must be a bead id such as nexus-qo7ie.34, with no space or comma; got " .. bead, 0)
end

if args.rdr ~= nil and (args.rdr < 1 or args.rdr > 9999) then
  error("rdr must be a positive integer up to 9999; got " .. args.rdr, 0)
end
if args.round ~= nil and (args.round < 1 or args.round > 99) then
  error("round must be an integer from 1 to 99; got " .. args.round, 0)
end

local date = string.match(args.now, "^(%d%d%d%d%-%d%d%-%d%d)T")
if date == nil then
  error("now must be an ISO 8601 UTC time; got " .. args.now, 0)
end

local body = args.body
if string.match(body, "^%s*$") then
  error("body must not be empty", 0)
end
-- A body that carries the agent's verdict block must agree with the outcome it records.
-- The block comes last, so an earlier quoted verdict (an example, a prior round) is not it:
-- the last "**outcome**" line is compared: with a "-" or "*" bullet or none, the colon
-- outside or inside the bold, the value bare, in backticks or in bold.
local stated = nil
for value in string.gmatch("\n" .. body, "\n[ \t]*[-*]?[ \t]*%*%*outcome:?%*%*:?[ \t]*[`*]*([a-z-]+)") do
  stated = value
end
if stated ~= nil and stated ~= args.outcome then
  error("the body's verdict '" .. stated .. "' disagrees with outcome '" .. args.outcome .. "'", 0)
end

local tags = "kind:gate,bead:" .. bead
local fields = "kind: " .. args.kind .. "\n" .. "bead: " .. bead .. "\n"
if args.rdr ~= nil then
  local rdr = string.format("%03d", args.rdr)
  tags = tags .. ",rdr:" .. rdr
  fields = fields .. "rdr: " .. rdr .. "\n"
end
fields = fields .. "sha: " .. sha .. "\n" .. "outcome: " .. args.outcome .. "\n" .. "date: " .. date .. "\n"

return {
  project = args.repo,
  title = args.kind .. "-" .. sha .. "-" .. bead .. "-" .. date
    .. (args.round ~= nil and ("-r" .. string.format("%d", args.round)) or ""),
  tags = tags,
  -- The record's own fields first: the reader takes the first match, so a line in the
  -- body never stands in for one of them.
  content = fields .. "\n" .. body,
}
