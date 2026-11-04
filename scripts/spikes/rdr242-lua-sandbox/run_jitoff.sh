#!/bin/sh
cd /w
for p in inst_while inst_hot_call; do
  timeout 30 /tmp/v/bin/python -I probe_limits.py luajit21 $p jitoff > /w/r.txt 2>&1; RC=$?
  echo "$p jitoff rc=$RC $(tail -1 /w/r.txt)"
done
