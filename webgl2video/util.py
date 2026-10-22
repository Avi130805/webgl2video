"""Small shared helpers: the log, durations, ffmpeg."""

import shutil
import subprocess
import sys
import time


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def first_line(e) -> str:
    s = str(e).strip()
    return (s.splitlines()[0] if s else type(e).__name__)[:300]


def fmt_dur(s: float) -> str:
    s = max(0, int(s))
    if s < 90:
        return f"{s} s"
    m = round(s / 60)
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # pip install imageio-ffmpeg (bundles a static ffmpeg)
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit("ffmpeg not found: install it (apt install ffmpeg, brew install ffmpeg) or `pip install imageio-ffmpeg`.")


def ffprobe_bin() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def run(cmd: list) -> str:
    """Run a tool; on failure raise with its command and the end of its output."""
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{pathlib_name(cmd[0])} failed:\n" + " ".join(map(str, cmd)) + "\n" + r.stdout[-4000:])
    return r.stdout


def pathlib_name(p) -> str:
    return str(p).replace("\\", "/").rsplit("/", 1)[-1]
