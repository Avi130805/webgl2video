#!/usr/bin/env python3
"""The scaling test's numbers, read from the workers' own logs (cloud/runpod/job.sh runs this).

  ladder.py stage <workspace> <tag> <start> <end> <job> [<job> ...]
      one stage (n jobs rendered at once): frames per second with the page loads, the steady rate (from the moment
      every job logged progress to the moment the first one finished; the gross rate when that window is shorter than
      LADDER_MIN_WINDOW seconds, 20), seconds per frame by job, the GPU (nvidia-smi dmon), the CPU (cpu_sample.py),
      the driver's STATS lines. Writes <workspace>/logs/stage-<tag>.txt
  ladder.py project <workspace> <frames> <frames done> <price $/h> <job start (epoch s)> <jobs> <jobs done>
                    [--budget DOLLARS]
      what the rest would take at each tested worker count: the rate, plus a page load a job (PAGE_LOAD_MIN, 1.0) and
      the join, the checks and the upload (TAIL_MIN, 12). With --budget, the last line is the decision: "AUTO go <n>"
      (the fastest tested count whose whole pod fits the budget; on rates within 5% of it, the most workers) or
      "AUTO stop <why>"
"""
import glob
import os
import re
import statistics as stt
import sys
import time

PROG = re.compile(r"^(\d\d):(\d\d):(\d\d)\s+(\d+)/(\d+) frames", re.M)


def secs(h, m, s):
    return int(h) * 3600 + int(m) * 60 + int(s)


def job_points(path):
    """The progress lines of a job's log as (seconds, frames done), the clock unwrapped across midnight."""
    t = open(path, errors="replace").read().replace("\r", "\n")
    pts, day, last = [], 0, None
    for h, m, s, k, _ in PROG.findall(t):
        x = secs(h, m, s)
        if last is not None and x + day < last - 43200:
            day += 86400
        last = x + day
        pts.append((last, int(k)))
    spf = re.findall(r"(\d+)/(\d+) frames · ([\d.]+) s per frame", t)
    return pts, (float(spf[-1][2]) if spf else 0.0), re.findall(r"STATS .*", t)


def stage(w, tag, st, et, jobs):
    n = len(jobs)
    frames = 0
    pts, spf, stats = {}, {}, []
    for j in jobs:
        a, b = j.split(":")
        frames += int(b) - int(a)
        p = f"{w}/logs/job-{a}-{b}.log"
        if os.path.exists(p):
            pts[j], spf[j], s = job_points(p)
            stats += s
            m = re.search(r"Resuming: (\d+) of \d+ frames", open(p, errors="replace").read())
            frames -= int(m.group(1)) if m else 0  # a resumed job: only what it rendered now counts
    if frames <= 0:
        out = f"STAGE {tag}: {n} workers, nothing rendered (every job was done before: a resumed run)"
        open(f"{w}/logs/stage-{tag}.txt", "w").write(out + "\n")
        print(out)
        return
    starts = [p[0][0] for p in pts.values() if p]
    ends = [p[-1][0] for p in pts.values() if p]
    steady, kind = None, "steady"
    if len(starts) == n and n:
        t0, t1 = max(starts), min(ends)
        if t1 - t0 >= float(os.environ.get("LADDER_MIN_WINDOW", "20")):
            done = 0
            for p in pts.values():
                at = lambda t: max([k for x, k in p if x <= t] or [0])
                done += at(t1) - at(t0)
            steady = done / (t1 - t0)
    if steady is None:  # jobs too short for a steady window: the rate with the page loads
        steady, kind = frames / max(0.1, et - st), "gross"
    dm = open(f"{w}/logs/dmon-{tag}.txt", errors="replace").read().splitlines() if os.path.exists(f"{w}/logs/dmon-{tag}.txt") else []
    cols, rows = None, []
    for line in dm:
        if line.startswith("# gpu"):
            cols = line[1:].split()
        elif cols and line.strip() and not line.startswith("#"):
            rows.append(dict(zip(cols, line.split())))

    def vals(k):
        return [float(r[k]) for r in rows if k in r and re.fullmatch(r"[\d.]+", r[k] or "")]

    avg = lambda k: f"{stt.mean(vals(k)):.0f}" if vals(k) else "-"
    mx = lambda k: f"{max(vals(k)):.0f}" if vals(k) else "-"
    cpu = open(f"{w}/logs/cpu-{tag}.txt").read().strip() if os.path.exists(f"{w}/logs/cpu-{tag}.txt") else "CPU: no sample"
    out = [f"STAGE {tag}: {n} workers, {frames} frames in {et - st:.1f} s ({frames / max(0.1, et - st):.2f} frames/s with page loads);"
           f" {kind} {steady:.2f} frames/s; s per frame by job: " + ", ".join(f"{j} {spf.get(j, 0):.2f}" for j in jobs),
           f"STAGE {tag} GPU: sm {avg('sm')}%, mem {avg('mem')}%, enc {avg('enc')}%, clocks {avg('pclk')}/{avg('mclk')} MHz,"
           f" power {avg('pwr')} W, PCIe rx {avg('rxpci')} tx {avg('txpci')} MB/s, video memory up to {mx('fb')} MB",
           f"STAGE {tag} {cpu}"] + [f"STAGE {tag} {s}" for s in stats[-3:]]
    open(f"{w}/logs/stage-{tag}.txt", "w").write("\n".join(out) + "\n")
    print("\n".join(out))


def project(w, f, done, price, t0, njobs, q, budget=None):
    page_load, tail = float(os.environ.get("PAGE_LOAD_MIN", "1.0")), float(os.environ.get("TAIL_MIN", "12"))
    fits = []
    for p in sorted(glob.glob(f"{w}/logs/stage-*.txt")):
        m = re.search(r"STAGE s\d+-w(\d+): .*?(steady|gross) ([\d.]+) frames/s", open(p).read())
        if not m:
            continue
        n, r = int(m.group(1)), float(m.group(3))
        mins = (f - done) / max(r, 1e-6) / 60 + (njobs - q) * page_load / n + tail
        so_far = (time.time() - t0) / 60
        cost = (so_far + mins) / 60 * price
        print(f"PROJECTION {n} workers: {r:.2f} frames/s {m.group(2)}; the rest about {mins:.0f} min; the whole pod about"
              f" {so_far + mins:.0f} min, ${cost:.2f} at ${price}/h")
        if budget is not None and cost <= budget:
            fits.append((n, r))
    if budget is not None:
        # the fastest count that fits; among counts within 5% of it (measurement noise), the most workers, so a tie
        # never picks fewer workers by chance
        top = max((r for _, r in fits), default=0)
        best = max((n for n, r in fits if r >= 0.95 * top), default=None)
        print(f"AUTO go {best}" if best else f"AUTO stop: no tested worker count fits the ${budget:.2f} budget")


if __name__ == "__main__":
    cmd, a = sys.argv[1], sys.argv[2:]
    if cmd == "stage":
        stage(a[0], a[1], float(a[2]), float(a[3]), a[4:])
    elif cmd == "project":
        budget = None
        if "--budget" in a:
            k = a.index("--budget")
            budget = float(a[k + 1])
            del a[k:k + 2]
        project(a[0], int(a[1]), int(a[2]), float(a[3]), int(a[4]), int(a[5]), int(a[6]), budget)
    else:
        sys.exit(__doc__)
