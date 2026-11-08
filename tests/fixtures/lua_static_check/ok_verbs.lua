-- grants: memory.get, memory.search
-- expect: ok
local get = memory.get
local hits = memory.search("lua", "nexus", 5)
local out = {}
for i, h in ipairs(hits) do out[i] = get("nexus", h.title).content end
return out
