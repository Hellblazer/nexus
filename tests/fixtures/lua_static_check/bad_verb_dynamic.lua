-- grants: memory.get
-- expect: ns-dynamic
local v = "get"
return memory[v]("p", "t")
