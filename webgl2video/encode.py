"""The ffmpeg side of a render: the encoders, a segment writer fed with raw frames, and the frame loop.

Every segment starts on a keyframe and uses identical settings (a keyframe every second, no B-frames), so segments
rendered anywhere, in any order, join later by stream copy and seek frame-exactly.
"""

import collections
import os
import pathlib
import subprocess
import sys
import time

from .util import ffmpeg_bin, log

BT709 = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
HDR_TAGS = ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv"]
SDR_PARAMS = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
HDR_PARAMS = "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv"

ENCODERS = ("auto", "nvenc", "videotoolbox", "x264", "x265")
DEFAULT_QUALITY = {"x264": 16, "x265": 16, "nvenc": 20, "videotoolbox": 78}


def gop(fps) -> int:
    return max(1, int(round(float(fps))))


# ---- SDR: H.264 ----------------------------------------------------------------------------------------------------

def x264(fps, crf: int, preset: str = "medium") -> list:
    g = gop(fps)
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high", *BT709,
            "-x264-params", f"keyint={g}:min-keyint={g}:scenecut=0"]


def videotoolbox_h264(fps, quality: int, mbps: int = None) -> list:
    """The Mac's hardware H.264 encoder: constant quality (1..100; 78 matched x264 CRF 16 on real frames), or a
    bitrate."""
    rate = ["-b:v", f"{mbps}M"] if mbps else ["-q:v", str(quality)]
    return ["-c:v", "h264_videotoolbox", *rate, "-profile:v", "high", *BT709, "-g", str(gop(fps)), "-bf", "0",
            "-allow_sw", "0"]


def nvenc_h264(fps, cq: int, mbps: int = None) -> list:
    """NVIDIA's hardware H.264 encoder: a quality target (0..51, lower is better) or a bitrate, under a 150 Mb/s cap."""
    rate = (["-b:v", f"{mbps}M", "-maxrate", f"{int(mbps * 1.5)}M", "-bufsize", f"{mbps * 2}M"] if mbps
            else ["-cq", str(cq), "-b:v", "0", "-maxrate", "150M", "-bufsize", "300M"])
    return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", *rate, "-profile:v", "high", *BT709,
            "-g", str(gop(fps)), "-bf", "0", "-spatial-aq", "1"]


# ---- HDR: HEVC Main 10 ---------------------------------------------------------------------------------------------

def x265(fps, crf: int, preset: str = "medium") -> list:
    """x265: Main 10, a closed GOP every second, the HDR10 QP optimisation. The mastering display and the light levels
    go in the container (webgl2video/hdr.py): given to x265, they come with a MaxCLL of 0 in the stream."""
    g = gop(fps)
    params = (f"keyint={g}:min-keyint={g}:scenecut=0:open-gop=0:colorprim=bt2020:transfer=smpte2084:"
              f"colormatrix=bt2020nc:range=limited:hdr10-opt=1:repeat-headers=1:log-level=error")
    return ["-c:v", "libx265", "-preset", preset, "-crf", str(crf), "-profile:v", "main10", "-tag:v", "hvc1",
            "-x265-params", params]


def videotoolbox_hevc(fps, quality: int, mbps: int = None) -> list:
    rate = ["-b:v", f"{mbps}M"] if mbps else ["-q:v", str(quality)]
    return ["-c:v", "hevc_videotoolbox", *rate, "-profile:v", "main10", "-g", str(gop(fps)), "-bf", "0",
            "-allow_sw", "0", "-tag:v", "hvc1", *HDR_TAGS]


def nvenc_hevc(fps, cq: int, mbps: int = None) -> list:
    rate = (["-b:v", f"{mbps}M", "-maxrate", f"{int(mbps * 1.5)}M", "-bufsize", f"{mbps * 2}M"] if mbps
            else ["-cq", str(cq), "-b:v", "0", "-maxrate", "150M", "-bufsize", "300M"])
    return ["-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", *rate, "-profile:v", "main10",
            "-g", str(gop(fps)), "-bf", "0", "-spatial-aq", "1", "-tag:v", "hvc1", *HDR_TAGS]


_works = {}


def encoder_works(name: str, hdr: bool) -> bool:
    """Does this ffmpeg have the hardware encoder, and does the hardware accept a tiny encode?"""
    key = (ffmpeg_bin(), name, hdr)
    if key not in _works:
        if name == "videotoolbox" and sys.platform != "darwin":
            _works[key] = False
            return False
        args = {("nvenc", False): nvenc_h264(30, 20), ("nvenc", True): nvenc_hevc(30, 20),
                ("videotoolbox", False): videotoolbox_h264(30, 78), ("videotoolbox", True): videotoolbox_hevc(30, 78)}[(name, hdr)]
        pix = "p010le" if hdr else "yuv420p"
        try:
            _works[key] = subprocess.run(
                [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x256:r=30",
                 "-frames:v", "2", "-vf", f"format={pix}", *args, "-f", "null", "-"],
                capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _works[key] = False
    return _works[key]


def pick_encoder(name: str, hdr: bool, fps, quality: int = None, mbps: int = None, preset: str = "medium",
                 software_render: bool = False) -> dict:
    """{name, tag, args, pix, label}. `auto`: NVENC where it works, else the Mac's hardware encoder, else x264 (SDR) or
    x265 (HDR) on the CPU. `pix` is what the encoder is fed (HDR; SDR feeds what the page sends)."""
    if name in ("auto", None):
        name = next((n for n in ("nvenc", "videotoolbox") if not software_render and encoder_works(n, hdr)),
                    "x265" if hdr else "x264")
    elif name in ("nvenc", "videotoolbox") and not encoder_works(name, hdr):
        raise RuntimeError(f"--encoder {name}: this ffmpeg or this machine has no working {name} encoder"
                           f" ({'HEVC Main 10' if hdr else 'H.264'})")
    if name == "x264" and hdr:
        name = "x265"
    if name == "x265" and not hdr:
        raise ValueError("--encoder x265 goes with --hdr (SDR is H.264: nvenc, videotoolbox or x264)")
    q = quality if quality is not None else DEFAULT_QUALITY[name]
    if name == "nvenc" and not 0 <= q <= 51:
        raise ValueError(f"NVENC's quality is 0..51 (lower is better), not {q}")
    rate = f"{mbps}M" if mbps and name in ("nvenc", "videotoolbox") else f"q{q}"
    if name == "x264":
        return {"name": name, "tag": f"x264-{preset}-crf{q}", "args": x264(fps, q, preset), "pix": None,
                "label": "x264 on the CPU"}
    if name == "x265":
        return {"name": name, "tag": f"hdr-x265-{preset}-crf{q}", "args": x265(fps, q, preset), "pix": "yuv420p10le",
                "label": "x265 on the CPU, 10-bit"}
    if name == "nvenc":
        return {"name": name, "tag": f"{'hdr-' if hdr else ''}nvenc-{rate}",
                "args": (nvenc_hevc if hdr else nvenc_h264)(fps, q, mbps), "pix": "p010le" if hdr else None,
                "label": f"NVIDIA's hardware {'HEVC encoder, 10-bit' if hdr else 'H.264 encoder'} (NVENC)"}
    if name == "videotoolbox":
        return {"name": name, "tag": f"{'hdr-' if hdr else ''}vt-{rate}",
                "args": (videotoolbox_hevc if hdr else videotoolbox_h264)(fps, q, mbps), "pix": "p010le" if hdr else None,
                "label": f"the Mac's hardware {'HEVC encoder, 10-bit' if hdr else 'H.264 encoder'} (VideoToolbox)"}
    raise ValueError(f"unknown encoder {name!r}")


# ffmpeg's RGB to Y'CbCr conversion with the chroma sited left, as H.264 and HEVC assume and as the page's GPU
# conversion sites it (swscale's default is the centre): the two paths then agree within two codes, not seven.
LEFT = "out_h_chr_pos=0:out_v_chr_pos=128"


def segment_cmd(cfg: dict, enc: dict, out) -> list:
    """ffmpeg reading the page's frames on stdin and writing one segment. NV12 and P010 frames arrive already in the
    encoder's own form, so ffmpeg only encodes them (and resizes them, when the render is larger than the delivery
    size); RGBA and 10-bit RGB frames are converted by its scale filter, BT.709 or BT.2020 limited range."""
    w, h, fps, fmt = cfg["width"], cfg["height"], cfg["fps"], cfg["format"]
    ow, oh = cfg.get("out_width", w), cfg.get("out_height", h)
    resize = (ow, oh) != (w, h)
    if fmt == "rgba":  # BT.709 matrix, as the stream is tagged (ffmpeg's default would be BT.601, which shifts colours)
        src, pix = "rgba", ["-pix_fmt", "yuv420p"]
        vf = f"scale={f'{ow}:{oh}:flags=lanczos:' if resize else ''}out_color_matrix=bt709:out_range=tv:{LEFT},{SDR_PARAMS}"
    elif fmt == "nv12":
        src, pix = "nv12", ["-pix_fmt", "nv12"]
        vf = (f"scale={ow}:{oh}:flags=lanczos," if resize else "") + SDR_PARAMS
    elif fmt == "x2bgr10":
        src, pix = "x2bgr10le", []
        vf = (f"scale={f'{ow}:{oh}:flags=lanczos:' if resize else ''}out_color_matrix=bt2020:out_range=tv:{LEFT},"
              f"format={enc['pix']},{HDR_PARAMS}")
    elif fmt == "p010":
        src, pix = "p010le", []
        vf = (f"scale={ow}:{oh}:flags=lanczos," if resize else "") + f"format={enc['pix']},{HDR_PARAMS}"
    else:
        raise ValueError(f"unknown frame format {fmt!r}")
    return [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", src, "-s", f"{w}x{h}",
            "-framerate", str(fps), "-i", "-", "-vf", vf, *enc["args"], *pix, "-r", str(fps), str(out)]


class SegmentWriter:
    """Streams raw frames into ffmpeg and writes one segment (to <name>.part.mp4, renamed when complete)."""

    def __init__(self, path, cfg: dict, enc: dict):
        self.path = pathlib.Path(path)
        self.tmp = self.path.with_name(self.path.stem + ".part" + self.path.suffix)
        self.errlog = self.path.with_name(self.path.stem + ".log")
        self._err = open(self.errlog, "wb")
        # P010 frames end in 16 bytes of light levels (page/render-page.js), which are not picture
        self.trailer = 16 if cfg["format"] == "p010" else 0
        self.proc = subprocess.Popen(segment_cmd(cfg, enc, self.tmp), stdin=subprocess.PIPE, stderr=self._err)

    def _error(self) -> str:
        self._err.flush()
        return self.errlog.read_text(errors="replace")[-2000:]

    def write(self, raw: bytes):
        try:
            self.proc.stdin.write(memoryview(raw)[:len(raw) - self.trailer] if self.trailer else raw)
        except (BrokenPipeError, OSError):
            raise RuntimeError("ffmpeg stopped:\n" + self._error()) from None

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait()
        self._err.close()
        if code != 0:
            raise RuntimeError("ffmpeg failed:\n" + self.errlog.read_text(errors="replace")[-2000:])
        os.replace(self.tmp, self.path)
        self.errlog.unlink()

    def abort(self):
        try:
            self.proc.kill()
            self.proc.wait()
        except Exception:
            pass
        self._err.close()
        for p in (self.tmp, self.errlog):
            try:
                p.unlink()
            except OSError:
                pass


STATS = {"browser": 0.0, "encoder": 0.0, "n": 0, "t": 0.0}


def stats_tick(engine):
    """Every 250 frames: the driver's waits per frame (on the browser, on ffmpeg) and the page's own stage times (ms,
    the mean of its last 64 frames), one log line. A cloud render's scaling test reads it."""
    STATS["n"] += 1
    if not STATS["t"]:
        STATS["t"] = time.time()
    if STATS["n"] < 250:
        return
    n, dt = STATS["n"], time.time() - STATS["t"]
    st = [x for x in getattr(engine, "stages", []) if isinstance(x, dict)]
    page = ", ".join(f"{k} {sum(x.get(k, 0) for x in st) / max(1, len(st)):.0f}" for k in ("render", "speculate", "read", "post"))
    log(f"STATS {n} frames in {dt:.1f} s ({n / max(dt, 1e-6):.2f} frames/s): waits per frame browser {STATS['browser'] / n:.3f} s,"
        f" encoder {STATS['encoder'] / n:.3f} s; page ms {page}")
    STATS.update(browser=0.0, encoder=0.0, n=0, t=time.time())


def render_frames(engine, frames, path, cfg: dict, enc: dict, on_frame=None, waits: dict = None,
                  until: float = None) -> int:
    """Render `frames` (indices, in order) into one segment at `path`; returns how many were written. The browser
    renders the next frame while ffmpeg takes the current one. `waits`, if given, accumulates the seconds spent waiting
    on the browser and on the encoder (else they go to the STATS line); `until` is a deadline (time.time()) after which
    no further frame is started."""
    frames = list(frames)
    writer = SegmentWriter(path, cfg, enc)
    done = 0
    if waits is None:
        waits = STATS
    nxt = lambda k: frames[k + 1] if k + 1 < len(frames) else None
    # Two requests are always queued on the browser's thread: the page starts rendering the frame after next the moment
    # it has read back the current one, so the GPU never waits for this loop, for the upload or for ffmpeg.
    futs = collections.deque(engine.frame_async(frames[k], nxt(k)) for k in range(min(2, len(frames))))
    queued = len(futs)
    try:
        for k, i in enumerate(frames):
            t = time.time()
            raw = engine.wait(futs.popleft())
            waits["browser"] += time.time() - t
            last = k + 1 == len(frames) or (until is not None and time.time() > until)
            if not last and queued < len(frames):
                futs.append(engine.frame_async(frames[queued], nxt(queued)))
                queued += 1
            t = time.time()
            writer.write(raw)
            waits["encoder"] += time.time() - t
            done += 1
            if waits is STATS:
                stats_tick(engine)
            if on_frame:
                on_frame(i, raw)
            if last:
                break
        writer.close()
    except BaseException:
        writer.abort()
        raise
    return done
