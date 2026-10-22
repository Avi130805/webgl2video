#!/usr/bin/env python3
"""The job queue of a cloud render: one job a line (A:B, frames A to B-1), in the order workers claim them.

  1. the scaling test's pieces: real jobs of the film, rendered 1, then 4, then N at a time (stage sizes on stderr as
     "STAGES 1 4 N"), spread across the film (a low-discrepancy pick), outside the heavy ranges
  2. the heavy ranges (--heavy "A:B A:B"), in job-sized pieces: the slowest work starts first, so it never sets the
     finish time (one heavy 2,000-frame chunk at 0.63 frames/s once ran 53 minutes after everything else was done)
  3. everything else, in order

Every job is --job frames (default 500), aligned on multiples of it: 2,000-frame jobs once left a 20-minute idle tail.

    queue.py <frames> <max workers> [--job 500] [--heavy "9840:10800 18960:20760"]
"""
import argparse
import sys

ap = argparse.ArgumentParser()
ap.add_argument("frames", type=int)
ap.add_argument("nmax", type=int)
ap.add_argument("--job", type=int, default=500)
ap.add_argument("--heavy", default="")
A = ap.parse_args()
n, S, nmax = A.frames, A.job, max(1, A.nmax)
if n < 1 or S < 1:
    sys.exit("queue: frames and --job must be positive")
heavy = []
for r in A.heavy.replace(",", " ").split():
    a, _, b = r.partition(":")
    heavy.append((int(a), int(b)))
blocks = [(a, min(a + S, n)) for a in range(0, n, S)]
hv = lambda blk: any(blk[0] < e and s < blk[1] for s, e in heavy)
stages = [1, 4, nmax] if nmax > 4 else ([1, nmax] if nmax > 1 else [1])
light = [blk for blk in blocks if not hv(blk)] or blocks
want = min(sum(stages), len(light))
picked, seen, k = [], set(), 0
while len(picked) < want:  # frac(k * golden ratio): each new piece lands in the largest gap left
    j = int(((k * 0.6180339887498949) % 1.0) * len(light))
    if j not in seen:
        seen.add(j)
        picked.append(light[j])
    k += 1
sizes, left = [], want
for s in stages:
    if left <= 0:
        break
    sizes.append(min(s, left))
    left -= sizes[-1]
jobs = picked + [blk for blk in blocks if hv(blk) and blk not in picked] + [blk for blk in blocks if not hv(blk) and blk not in picked]
print("\n".join(f"{a}:{b}" for a, b in jobs))
print("STAGES " + " ".join(map(str, sizes)), file=sys.stderr)
