-- procedure: research_add
-- summary: Build an RDR research record; the caller writes it with memory_put.
-- args: rdr:integer, classification:string, method:string, finding:string, repo:string
-- grants: memory.list, tables.get

-- RDR-240 Technical Design 7 (nexus-qo7ie.33). Checks the classification and the
-- method against the rdr-research-vocab table, takes the next sequence number after
-- the highest existing "<id>-research-<seq>" title of the RDR, and returns the record
-- for the caller to write with memory_put. Nothing here writes: two adds to one RDR at
-- the same moment can take the same number, so the caller reads the title back before
-- writing and runs this again if it is taken. The finding is returned as given; it is
-- a value in a table, never shell text. The record is the one `nx rdr preamble
-- rdr-research -- add` writes: title, content lines and tags.

local args = ...

local vocab = tables.get("rdr-research-vocab")

local function check(dimension, value)
  local domain = vocab.dimensions[dimension].domain
  for _, allowed in ipairs(domain) do
    if allowed == value then
      return
    end
  end
  error(dimension .. " must be one of " .. table.concat(domain, ", ") .. "; got " .. value, 0)
end

check("classification", args.classification)
check("method", args.method)
if args.rdr < 1 then
  error("rdr must be a positive integer; got " .. args.rdr, 0)
end
-- Trimmed as `nx rdr` trims it; one line, because the record is read line by line and a
-- line such as "acknowledged: true" inside a finding would read as the record's own field.
local finding = string.match(args.finding, "^%s*(.-)%s*$")
if finding == "" then
  error("finding must not be empty", 0)
end
if string.find(finding, "[\r\n]") then
  error("finding must be one line; it holds a line break", 0)
end

-- Titles carry the id padded to three digits ("097-research-9"); any zero padding
-- of the same number is the same RDR, as the gate reads it. A title may carry a
-- ": <summary>" suffix after the sequence number ("204-research-16: the bullet").
local key = string.format("%03d", args.rdr)
local project = args.repo .. "_rdr"
local pattern = "^0*" .. string.format("%d", args.rdr) .. "%-research%-(%d+)"

local highest = 0
for _, entry in ipairs(memory.list(project)) do
  local title = entry.title or ""
  local seq = string.match(title, pattern .. "$") or string.match(title, pattern .. ":")
  if seq ~= nil then
    local n = math.tointeger(tonumber(seq))
    if n == nil or n >= math.maxinteger then
      error("research title " .. title .. " has a sequence number out of range", 0)
    end
    if n > highest then
      highest = n
    end
  end
end
local seq = highest + 1

local content = "rdr_id: " .. key .. "\n"
  .. "seq: " .. string.format("%d", seq) .. "\n"
  .. "classification: " .. args.classification .. "\n"
  .. "verification_method: " .. args.method .. "\n"
  .. "finding: " .. finding .. "\n"

return {
  project = project,
  title = key .. "-research-" .. string.format("%d", seq),
  content = content,
  tags = "rdr,research",
}
