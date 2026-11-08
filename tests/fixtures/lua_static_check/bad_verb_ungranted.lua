-- grants: memory.get
-- expect: verb-ungranted
local v = memory.get("p", "t")
return memory.list("p")
