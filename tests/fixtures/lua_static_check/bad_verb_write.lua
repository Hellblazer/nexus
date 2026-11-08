-- grants: memory.get
-- expect: verb-unknown
memory.put("p", "t", "x")
return memory.get("p", "t")
