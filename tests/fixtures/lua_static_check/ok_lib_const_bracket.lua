-- expect: ok
-- a constant key in brackets compiles to the same static field access as string.rep
return string["rep"]("x", 2)
