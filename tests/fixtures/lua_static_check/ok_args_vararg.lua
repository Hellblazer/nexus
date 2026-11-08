-- grants: memory.get
-- expect: ok
local args = ...
local function count(...) return select("#", ...) end
return {memory.get(args.project, args.title), count(args)}
