-- grants: memory.get
-- expect: ns-dynamic
return memory["g" .. "et"]("p", "t")
