-- grants: memory.get
-- expect: ns-value
memory.get = function() return "forged" end
return memory.get("p", "t")
