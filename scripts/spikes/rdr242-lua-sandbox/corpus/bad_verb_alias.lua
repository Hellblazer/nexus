-- grants: memory.get
-- expect: ns-value
local m = memory
return m.get("p", "t")
