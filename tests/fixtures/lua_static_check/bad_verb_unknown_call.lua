-- grants: memory.get
-- expect: verb-unknown
memory.get("p", "t")
return memory.put("p", "t", "x")
