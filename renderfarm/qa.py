"""python -m renderfarm qa <work dir> <A> <B> <out dir>: check a rendered range of a film from its segments.

Every frame is decoded small (NVIDIA's decoder when ffmpeg has it) and compared with the one before as the mean
absolute difference of its grey levels (0-255). The seams are checked too: the range's first frame against the last
frame of the segment before it, and its last frame against the first frame of the segment after it, when those are
there (rendered by other jobs: a worker that drew differently shows at a seam; whichever of two neighbouring jobs
finishes second checks the seam between them). A difference above --cut-at at a frame that is not a known hard cut is
flagged as a cut; a frame that differs sharply from both neighbours while they match each other is flagged as a flash
(a single wrong frame: a page that was not ready, a GPU that dropped a pass). Writes a sheet of stills (the range's
start, every 400 frames, either side of each shot start, its end) and prints one JSON line, also written to
<out>/check-A-B.json: {range, frames, decoder, max, top, known_cuts, flags, stills}.

    python -m renderfarm qa .renderfarm/work/x 0 600 qa/ --cuts 120 [--starts 0,300] [--label w1-]
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

from .util import ffmpeg_bin


def main(argv: list):
    ap = argparse.ArgumentParser(prog="python -m renderfarm qa", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("a", type=int)
    ap.add_argument("b", type=int)
    ap.add_argument("out")
    ap.add_argument("--cuts", default="", help="the film's known hard cuts (frame numbers), comma-separated")
    ap.add_argument("--starts", default="", help="other shot starts (continuing shots): a still either side")
    ap.add_argument("--label", default="", help="a prefix for the files written")
    ap.add_argument("--cut-at", type=float, default=20.0, help="the grey-level difference that counts as a cut")
    ap.add_argument("--size", default="480x270", help="the size frames are compared at")
    A = ap.parse_args(argv)
    import numpy as np

    work, out = pathlib.Path(A.work), pathlib.Path(A.out)
    out.mkdir(parents=True, exist_ok=True)
    W, H = map(int, A.size.split("x"))
    cuts = {int(x) for x in A.cuts.split(",") if x}
    starts = {int(x) for x in A.starts.split(",") if x} | cuts

    # the segments that hold [a, b), in order
    segs = []
    for p in sorted(work.glob("seg_*.mp4")):
        m = re.fullmatch(r"seg_(\d+)-(\d+)\.mp4", p.name)
        if m and int(m.group(1)) < A.b and int(m.group(2)) > A.a:
            segs.append((int(m.group(1)), int(m.group(2)), p))
    if not segs or segs[0][0] > A.a or segs[-1][1] < A.b or any(segs[i][1] != segs[i + 1][0] for i in range(len(segs) - 1)):
        sys.exit(f"qa: the segments in {work} do not cover {A.a}:{A.b} without gaps")
    first = segs[0][0]
    lst = out / f"{A.label}list-{A.a}-{A.b}.txt"
    lst.write_text("".join("file '{}'\n".format(str(p.resolve()).replace("'", "'\\''")) for _, _, p in segs))

    def gray(rgb):
        return rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)

    # the frame before A (the last of the segment that ends at A, decoded from its last second only) and the frame at B
    # (the first of the segment that starts there)
    n_bytes = W * H * 3

    def one_frame(seg, last):
        if seg is None:
            return None
        r = subprocess.run([ffmpeg_bin(), "-v", "error", *(["-sseof", "-1.5"] if last else []), "-i", str(seg),
                            *([] if last else ["-frames:v", "1"]), "-vf", f"scale={W}:{H}:flags=area,format=rgb24",
                            "-f", "rawvideo", "-"], capture_output=True)
        buf = r.stdout[-n_bytes:] if last else r.stdout[:n_bytes]
        return gray(np.frombuffer(buf, np.uint8).reshape(H, W, 3)) if len(buf) == n_bytes else None
    before = one_frame(next(iter(work.glob(f"seg_*-{A.a:06d}.mp4")), None) if A.a > 0 else None, True)
    after = one_frame(next((p for p in work.glob(f"seg_{A.b:06d}-*.mp4") if re.fullmatch(r"seg_\d+-\d+\.mp4", p.name)), None), False)

    def decode(hw):
        cmd = [ffmpeg_bin(), "-v", "error", *(["-hwaccel", "cuda"] if hw else []), "-f", "concat", "-safe", "0",
               "-i", str(lst), "-vf", f"scale={W}:{H}:flags=area,format=rgb24", "-f", "rawvideo", "-"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    still_at = sorted({A.a, *range(A.a + 400, A.b, 400), A.b - 1}
                      | {f for c in starts if A.a < c < A.b for f in (c - 1, c)})
    still_at = [f for f in still_at if A.a <= f < A.b]
    stills, diffs, flashes = {}, [], []
    decoder, count = None, 0
    for hw in (True, False):
        proc = decode(hw)
        i = first
        stills.clear()
        diffs.clear()
        flashes.clear()
        prev, prev2, count = before, None, 0
        while True:
            buf = proc.stdout.read(n_bytes)
            if len(buf) < n_bytes:
                break
            if i >= A.a:
                rgb = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                g = gray(rgb)
                count += 1
                if prev is not None:
                    diffs.append((i, float(np.abs(g - prev).mean())))
                    if prev2 is not None:
                        d1, d2 = diffs[-2][1] if len(diffs) > 1 else 0, diffs[-1][1]
                        if d1 > A.cut_at and d2 > A.cut_at and float(np.abs(g - prev2).mean()) < A.cut_at / 2:
                            flashes.append(i - 1)
                if i in still_at:
                    stills[i] = rgb.copy()
                prev2, prev = prev, g
            i += 1
            if i >= A.b:
                break
        proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.wait()
        if i >= A.b:
            decoder = "cuda" if hw else "cpu"
            break
        if not hw:
            sys.exit(f"qa: decoded only up to frame {i} of {A.a}:{A.b}: {err[-300:]}")
    lst.unlink()
    if after is not None and prev is not None:  # the seam at B
        diffs.append((A.b, float(np.abs(after - prev).mean())))
        if prev2 is not None and diffs[-2][0] == A.b - 1 and diffs[-2][1] > A.cut_at and diffs[-1][1] > A.cut_at \
                and float(np.abs(after - prev2).mean()) < A.cut_at / 2:
            flashes.append(A.b - 1)

    flags = [{"frame": f, "kind": "cut", "diff": round(d, 1)} for f, d in diffs if d > A.cut_at and f not in cuts]
    flags += [{"frame": f, "kind": "flash"} for f in flashes if f not in cuts and f + 1 not in cuts]
    known = [{"frame": f, "diff": round(d, 1)} for f, d in diffs if f in cuts]
    top = sorted(diffs, key=lambda x: -x[1])[:5]

    # the sheet: the stills in rows of up to 8 (drawn with ffmpeg: no fonts or imaging library needed)
    tiles = [stills[f] for f in still_at if f in stills]
    if tiles:
        cols = min(8, len(tiles))
        rows = -(-len(tiles) // cols)
        sheet = np.zeros((rows * (H + 4), cols * (W + 4), 3), np.uint8)
        for k, t in enumerate(tiles):
            r, c = divmod(k, cols)
            sheet[r * (H + 4):r * (H + 4) + H, c * (W + 4):c * (W + 4) + W] = t
        subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", f"{sheet.shape[1]}x{sheet.shape[0]}", "-i", "-", "-q:v", "4",
                        str(out / f"{A.label}sheet-{A.a}-{A.b}.jpg")], input=sheet.tobytes(), check=True)
    res = {"range": f"{A.a}:{A.b}", "frames": count, "seams": [before is not None, after is not None], "decoder": decoder,
           "max": round(max((d for _, d in diffs), default=0), 1), "top": [[f, round(d, 1)] for f, d in top],
           "known_cuts": known, "flags": flags, "stills": still_at}
    (out / f"{A.label}check-{A.a}-{A.b}.json").write_text(json.dumps(res) + "\n")
    print(json.dumps(res), flush=True)
