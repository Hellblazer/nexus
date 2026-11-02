#!/bin/sh
cd /w
M=$1
echo "== escape $M"; /tmp/v/bin/python -I probe_escape.py $M 2>&1
echo "== leak/bytecode $M"; /tmp/v/bin/python -I leak_check.py $M 2>&1
for p in inst_while inst_forloop inst_hot_call mem_string mem_table stack_recursion result_size coroutine_hook; do
  echo -n "$M $p jit-on: "; timeout 30 /tmp/v/bin/python -I probe_limits.py $M $p 2>&1 | tail -1; echo " rc=$?"
done
for p in inst_while inst_forloop inst_hot_call; do
  echo -n "$M $p jit-off: "; timeout 30 /tmp/v/bin/python -I probe_limits.py $M $p jitoff 2>&1 | tail -1; echo " rc=$?"
done
