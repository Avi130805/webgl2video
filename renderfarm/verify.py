"""python -m renderfarm verify <video>: check a finished video before it leaves the machine.

  PROBE      the streams (codec, size, frame rate, frames counted, duration) and the file's size and bitrate
  LOUDNESS   integrated loudness (EBU R128) and true peak of the soundtrack
  SYNC       the video's sound against the source soundtrack (--audio), cross-correlated over a window at three points:
             the offset in milliseconds (0 when the join kept them aligned)
  STILLS     evenly spaced stills (1920 wide, JPEG) and, with --full, chosen frames at full size (PNG, for a 1:1 look)

Exits 1 if the video does not hold --expect-frames frames.

    python -m renderfarm verify film.mp4 --expect-frames 36000 --audio soundtrack.wav --stills checks/ [--full 600,1800]
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

from .util import ffmpeg_bin, ffprobe_bin


def q(*a) -> str:
    return subprocess.run([str(x) for x in a], capture_output=True, text=True).stdout.strip()


def pcm(src, t: float, dur: float):
    import numpy as np
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                          "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)


def main(argv: list):
    ap = argparse.ArgumentParser(prog="python -m renderfarm verify", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--expect-frames", type=int)
    ap.add_argument("--audio", help="the source soundtrack, for the sync check")
    ap.add_argument("--stills", help="a folder for the stills")
    ap.add_argument("--n-stills", type=int, default=12)
    ap.add_argument("--full", default="", help="frames to save at full size (PNG), comma-separated")
    A = ap.parse_args(argv)
    import numpy as np

    m = pathlib.Path(A.video)
    v = json.loads(q(ffprobe_bin(), "-v", "error", "-count_packets", "-show_entries",
                     "stream=codec_type,codec_name,profile,pix_fmt,width,height,r_frame_rate,nb_read_packets,duration,"
                     "channels,sample_rate,color_transfer,color_primaries", "-show_entries", "format=duration,size,bit_rate",
                     "-of", "json", m) or "{}")
    print("PROBE", json.dumps(v), flush=True)
    vs = next((s for s in v.get("streams", []) if s.get("codec_type") == "video"), None)
    if vs is None:
        sys.exit("no video stream")
    frames = int(vs.get("nb_read_packets") or 0)
    a, b = (int(x) for x in vs["r_frame_rate"].split("/"))
    fps = a / b
    dur = frames / fps
    has_audio = any(s.get("codec_type") == "audio" for s in v.get("streams", []))
    if has_audio:
        err = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(m), "-map", "0:a", "-af",
                              "ebur128=peak=true:framelog=quiet", "-f", "null", "-"], capture_output=True, text=True).stderr
        tail = err[err.rfind("Summary:"):]
        lufs = re.search(r"I:\s+(-?[\d.]+) LUFS", tail)
        tp = re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail)
        print("LOUDNESS", lufs and lufs.group(1), "LUFS, true peak", tp and tp.group(1), "dBTP", flush=True)
    if A.audio and has_audio:
        win = max(0.5, min(10.0, dur / 4))
        for t in (0.15 * dur, 0.5 * dur, max(0.0, 0.85 * dur - win)):
            x, y = pcm(m, t, win), pcm(A.audio, t, win)
            n = min(len(x), len(y))
            if n < 800 or not x[:n].any() or not y[:n].any():
                print(f"SYNC at {t:.1f} s: silent here, not measured", flush=True)
                continue
            c = np.fft.irfft(np.fft.rfft(x[:n], 2 * n) * np.conj(np.fft.rfft(y[:n], 2 * n)))
            k = int(np.argmax(c))
            k = k - 2 * n if k > n else k
            print(f"SYNC at {t:.1f} s: offset {1000 * k / 8000:+.1f} ms", flush=True)
    if A.stills:
        d = pathlib.Path(A.stills)
        d.mkdir(parents=True, exist_ok=True)
        picks = sorted({int(frames * (k + 0.5) / A.n_stills) for k in range(A.n_stills)}) if frames else []
        for f in picks:
            subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-ss", f"{f / fps:.6f}", "-i", str(m), "-frames:v", "1",
                            "-vf", "scale='min(1920,iw)':-2", "-q:v", "3", str(d / f"f{f:06d}.jpg")])
        full = [int(x) for x in A.full.split(",") if x.strip()]
        for f in full:
            subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-ss", f"{f / fps:.6f}", "-i", str(m), "-frames:v", "1",
                            str(d / f"full-f{f:06d}.png")])
        print("STILLS", len(picks), "evenly spaced;", len(full), "full-size", flush=True)
    if A.expect_frames is not None and frames != A.expect_frames:
        print(f"FRAMES {frames}, not {A.expect_frames}", flush=True)
        sys.exit(1)
    print(f"FRAMES {frames} ({dur:.2f} s at {fps:g} fps)", flush=True)
