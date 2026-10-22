#!/usr/bin/env python3
"""On the pod: frames rendered in the last 2 minutes across every running job (from their progress lines), one line.
logserver.py runs it over SSH every 20 s.  rate.py [<workspace>, default /workspace]"""
import glob
import re
import sys
import time

w = sys.argv[1] if len(sys.argv) > 1 else "/workspace"
now = time.localtime()  # the jobs log local time (UTC on a pod)
t_now = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
win, done_now, done_then, active, total = 120, 0, 0, 0, 0
for f in glob.glob(f"{w}/logs/job-*.log"):
    t = open(f, errors="replace").read().replace("\r", "\n")
    pts = [(int(h) * 3600 + int(m) * 60 + int(s), int(k), int(n))
           for h, m, s, k, n in re.findall(r"^(\d\d):(\d\d):(\d\d)\s+(\d+)/(\d+) frames", t, re.M)]
    if not pts:
        continue
    n = pts[-1][2]
    total += pts[-1][1]
    at = lambda T: max([k for ts, k, _ in pts if ts <= T] or [0])
    done_now += at(t_now)
    done_then += at(t_now - win)
    if pts[-1][1] < n and t_now - pts[-1][0] < 60:
        active += 1
print(f"{time.strftime('%H:%M', now)}: {active} jobs rendering, {(done_now - done_then) / win:.2f} frames/s overall over"
      f" the last 2 min; {total} frames rendered so far")
