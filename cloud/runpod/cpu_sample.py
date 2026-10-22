#!/usr/bin/env python3
"""Where a pod's CPU goes while it renders: every 2 s, each process's CPU time (/proc/<pid>/stat), summed by kind (the
browser's page, its GPU process, its other processes, the render driver, ffmpeg, the rest). Stops on SIGTERM and
writes one line: the cores each kind used over the run. Linux only.

    cpu_sample.py <out file> &   ...   kill %1
"""
import os
import re
import signal
import sys
import time

out = sys.argv[1]
tick = os.sysconf("SC_CLK_TCK")
seen, total, t0, stop, first = {}, {}, time.time(), False, True


def kind(cmd):
    if "renderfarm" in cmd and "python" in cmd:
        return "driver"
    if re.search(r"(^|/)ffmpeg\b", cmd):
        return "ffmpeg"
    if "chrome" in cmd or "headless_shell" in cmd:
        m = re.search(r"--type=([a-z-]+)", cmd)
        t = m.group(1) if m else "browser"
        return {"renderer": "page", "gpu-process": "gpu-process"}.get(t, "chrome-" + t)
    return "other"


def sample():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                st = f.read()
            cpu = sum(int(x) for x in st[st.rfind(")") + 2:].split()[11:13]) / tick
            if pid not in seen:  # a process already running at the start counts from there; one born later, from 0
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    k = kind(f.read().replace(b"\0", b" ").decode("utf-8", "replace"))
                seen[pid] = [k, cpu if first else 0.0]
            k, last = seen[pid]
            total[k] = total.get(k, 0.0) + (cpu - last)
            seen[pid][1] = cpu
        except (OSError, ValueError, IndexError):
            continue


def end(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, end)
sample()
first = False
while not stop:
    time.sleep(2)
    sample()
dt = max(1e-6, time.time() - t0)
with open(out, "w") as f:
    f.write(f"CPU over {dt:.0f} s, cores: " + ", ".join(f"{k} {v / dt:.2f}" for k, v in sorted(total.items(), key=lambda x: -x[1]))
            + f"; all {sum(total.values()) / dt:.2f}\n")
