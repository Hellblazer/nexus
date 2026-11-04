#!/bin/sh
cd /w
for p in inst_while inst_forloop inst_hot_call coroutine_hook; do
  for mode in jiton jitoff; do
    if [ $mode = jitoff ]; then X=jitoff; else X=; fi
    timeout 30 /tmp/v/bin/python -I probe_limits.py luajit21 $p $X > /w/r.txt 2>&1; RC=$?
    echo "$p $mode rc=$RC $(tail -1 /w/r.txt)"
  done
done
for p in inst_while inst_forloop coroutine_hook; do
  timeout 30 /tmp/v/bin/python -I probe_limits.py lua54 $p > /w/r.txt 2>&1; RC=$?
  echo "lua54 $p rc=$RC $(tail -1 /w/r.txt)"
done
