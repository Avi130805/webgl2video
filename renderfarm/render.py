"""python -m renderfarm render <page>: render a deterministic WebGL page into a video, frame-exactly.

It renders resumable segments (a keyframe every second, no B-frames) into a work folder, then joins them by stream copy
with the soundtrack. Interrupted, the same command continues where it stopped; --frames A:B --segments-only renders one
piece of the film (a cloud worker's job), and a run without --frames joins every piece.
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

from . import hdr as hdrlib
from .browser import CPU_PROFILE, Engine, EngineTimeout, PageStop, default_root, pick_profile
from .encode import ENCODERS, pick_encoder, render_frames
from .util import ffmpeg_bin, first_line, fmt_dur, log, run


class Progress:
    """A progress line: rewritten in place on a terminal; in a log, one line every RENDERFARM_PROGRESS_SECONDS (30)."""

    def __init__(self, total: int, done: int):
        self.total, self.done, self.base = total, done, done
        self.t0 = self.last = time.time()
        self.tty = sys.stdout.isatty()
        self.every = float(os.environ.get("RENDERFARM_PROGRESS_SECONDS", "30"))

    def tick(self):
        self.done += 1
        spf = (time.time() - self.t0) / (self.done - self.base)
        msg = (f"  {self.done}/{self.total} frames · {spf:.2f} s per frame"
               f" · about {fmt_dur((self.total - self.done) * spf)} left")
        if self.tty:
            print("\r" + msg + "     ", end="", flush=True)
        elif time.time() - self.last > self.every or self.done == self.total:
            self.last = time.time()
            log(msg)

    def end(self):
        if self.tty and self.done > self.base:
            print(flush=True)


def parse_size(s: str):
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", s or "")
    if not m:
        sys.exit(f"bad size {s!r}: WxH, e.g. 3840x2160")
    return int(m.group(1)), int(m.group(2))


def frame_range(spec: str, n: int):
    if not spec:
        return 0, n
    m = re.fullmatch(r"(\d+):(\d+)", spec)
    if not m:
        sys.exit(f"--frames {spec!r}: A:B, e.g. 0:600")
    a, b = max(0, int(m.group(1))), min(n, int(m.group(2)))
    if a >= b:
        sys.exit(f"--frames {spec}: no frames of 0:{n} in that range")
    return a, b


def write_json_atomic(path: pathlib.Path, data: dict):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def claim_work_dir(work: pathlib.Path, settings: dict):
    """The work folder holds the segments of one page and one set of settings (render.json): stop rather than mix."""
    work.mkdir(parents=True, exist_ok=True)
    meta = work / "render.json"
    for _ in range(5):
        if not meta.exists():
            write_json_atomic(meta, settings)
            return
        try:
            have = json.loads(meta.read_text())
            break
        except ValueError:  # another worker is writing it
            time.sleep(0.2)
    else:
        sys.exit(f"{meta} is not readable")
    if have != settings:
        diff = [f"{k}: {have.get(k)!r} there, {v!r} now" for k, v in settings.items() if have.get(k) != v]
        sys.exit(f"{work} holds segments of another page or other settings ({'; '.join(diff)[:600]}).\n"
                 "Use another --work folder, or delete that one to start again.")


def assemble_sdr(segments: list, audio, out: pathlib.Path, fps, frames: int, tmp_dir: pathlib.Path,
                 audio_offset: float = 0.0):
    """Join the segments without re-encoding and add the soundtrack (AAC, 320 kb/s): an MP4 that starts playing before
    it has all arrived (+faststart)."""
    lst = tmp_dir / f"segments-{out.stem}.txt"
    lst.write_text("".join("file '{}'\n".format(str(pathlib.Path(p).resolve()).replace("'", "'\\''")) for p in segments))
    part = out.with_name(out.stem + ".part" + out.suffix)
    sound = ([*(["-ss", f"{audio_offset:.3f}"] if audio_offset else []), "-i", str(audio)] if audio else [])
    run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), *sound,
         "-map", "0:v:0", *(["-map", "1:a:0", "-c:a", "aac", "-b:a", "320k", "-ar", "48000"] if audio else []),
         "-c:v", "copy", "-t", f"{frames / float(fps):.6f}", "-movflags", "+faststart", str(part)])
    os.replace(part, out)
    lst.unlink()


def main(argv: list):
    ap = argparse.ArgumentParser(prog="python -m renderfarm render", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page", help="the page (an .html file that follows docs/page-contract.md)")
    ap.add_argument("--root", help="the folder served to the browser (default: the current folder if it holds the page,"
                                   " else the page's folder)")
    ap.add_argument("--size", default="1920x1080", help="the delivery size, WxH (default 1920x1080)")
    ap.add_argument("--scale", type=float, default=1.0, help="render at this multiple of --size and downsample with"
                                                              " Lanczos (supersampling; default 1)")
    ap.add_argument("--fps", type=float, help="ask the page for this frame rate (default: the page's own)")
    ap.add_argument("--hdr", action="store_true", help="HDR10: 10-bit PQ BT.2020 frames, HEVC Main 10, an MKV with the"
                                                       " HDR10 metadata (needs MKVToolNix)")
    ap.add_argument("--convert", choices=("gpu", "cpu"), default="gpu",
                    help="where RGB becomes Y'CbCr: in the page on the GPU (NV12, P010; default) or in ffmpeg")
    ap.add_argument("--encoder", default="auto", choices=ENCODERS)
    ap.add_argument("--quality", type=int, help="x264/x265 CRF (default 16), NVENC CQ 0-51 (20), VideoToolbox 1-100 (78)")
    ap.add_argument("--bitrate", type=int, metavar="MBPS", help="a bitrate for the hardware encoders instead of a quality")
    ap.add_argument("--preset", default="medium", help="x264/x265 speed preset")
    ap.add_argument("--frames", default="", help="render only frames A:B")
    ap.add_argument("--segment", type=int, default=300, help="frames per resumable segment (default 300)")
    ap.add_argument("--work", help="the segments' folder (default .renderfarm/work/<page>-<hash>)")
    ap.add_argument("--out", help="the video file (default <page>-<H>p<fps>.mp4; .mkv with --hdr)")
    ap.add_argument("--segments-only", action="store_true", help="render the segments and stop (no join)")
    ap.add_argument("--audio", default="auto", help="'auto' (the page's soundtrack, if it has one), 'page', 'none',"
                                                     " or a sound file")
    ap.add_argument("--param", action="append", default=[], metavar="K=V", help="a parameter for the page (repeatable)")
    ap.add_argument("--cpu", action="store_true", help="render with SwiftShader (the CPU) even where a GPU works")
    ap.add_argument("--require-gpu", action="store_true", help="stop if the browser cannot use the GPU, rather than"
                                                                " render on the CPU (a paid cloud machine)")
    ap.add_argument("--keep", action="store_true", help="keep the segments after the join")
    ap.add_argument("--hdr-lut", help="with --hdr, a .cube LUT to attach (YouTube makes the SDR version with it)")
    ap.add_argument("--consume-segments", action="store_true", help="with --hdr, for a small disk: the join deletes each"
                    " segment once it has read it (a failed join re-renders them)")
    args = ap.parse_args(argv)

    page = pathlib.Path(args.page).expanduser().resolve()
    if not page.is_file():
        sys.exit(f"no page at {page}")
    root = pathlib.Path(args.root).expanduser().resolve() if args.root else default_root(page)
    if root not in page.parents:
        sys.exit(f"--root {root} does not hold the page {page}")
    for kv in args.param:
        if not re.fullmatch(r"[A-Za-z_][\w.-]*=.*", kv):
            sys.exit(f"--param {kv!r}: K=V")
    if args.frames and not re.fullmatch(r"\d+:\d+", args.frames):
        sys.exit(f"--frames {args.frames!r}: A:B, e.g. 0:600")
    if args.consume_segments and (args.keep or not args.hdr):
        sys.exit("--consume-segments goes with --hdr, and not with --keep.")
    if args.hdr_lut and not args.hdr:
        sys.exit("--hdr-lut goes with --hdr.")
    if args.hdr and not args.segments_only and not shutil.which("mkvpropedit"):
        sys.exit("--hdr: the HDR10 master is written with MKVToolNix (apt install mkvtoolnix, brew install mkvtoolnix);"
                 " install it, or render the segments with --segments-only and join them where it is installed.")
    ow, oh = parse_size(args.size)
    even = lambda x: int(round(x / 2)) * 2
    w, h = even(ow * args.scale), even(oh * args.scale)
    fmt = ("p010" if args.convert == "gpu" else "x2bgr10") if args.hdr else ("nv12" if args.convert == "gpu" else "rgba")
    if fmt == "nv12" and w % 4:
        log(f"NV12 needs a width divisible by 4 ({w} is not): ffmpeg converts the frames instead")
        fmt = "rgba"
    cfg = dict(width=w, height=h, out_width=ow, out_height=oh, format=fmt, fps=args.fps, params=args.param)

    profile = None
    if args.cpu:
        log("Rendering on the CPU (SwiftShader), as asked.")
    else:
        log("Checking whether the browser can use this machine's GPU ...")
        profile, pinfo = pick_profile(page, root, cfg)
        log(f"  yes: {pinfo['gl']}" if profile else "  no hardware WebGL here: rendering on the CPU (SwiftShader), much slower.")
        if not profile and args.require_gpu:
            sys.exit("--require-gpu: no hardware WebGL here; stopping rather than rendering on the CPU.")

    log(f"Loading {page.name} ({w}x{h}, {fmt}) ...")
    engine = Engine(page, root, cfg, profile or CPU_PROFILE, require_hw=profile is not None)
    try:
        info = engine.open()
        fps = info["fps"]
        cfg["fps"] = fps
        n = info["frames"]
        a, b = frame_range(args.frames, n)
        enc = pick_encoder(args.encoder, args.hdr, fps, args.quality, args.bitrate, args.preset,
                           software_render=profile is None)
        log(f"Page: {n} frames at {fps} fps ({n / fps:.1f} s); WebGL: {info['gl']}")
        log(f"Encoder: {enc['label']} ({enc['tag']}); rendering at {w}x{h}, delivering {ow}x{oh}")
        settings = {"key": engine.key(), "page": page.relative_to(root).as_posix(), "size": [w, h], "out": [ow, oh],
                    "fps": fps, "format": fmt, "params": args.param, "segment": args.segment, "encoder": enc["tag"]}
        tag = hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]
        work = (pathlib.Path(args.work).expanduser().resolve() if args.work
                else pathlib.Path.cwd() / ".renderfarm" / "work" / f"{page.stem}-{tag}")
        claim_work_dir(work, settings)
        segs = []
        s = a
        while s < b:  # segments aligned on multiples of --segment, so pieces rendered apart never overlap
            e = min((s // args.segment + 1) * args.segment, b)
            segs.append((s, e))
            s = e
        seg = lambda s, e: work / f"seg_{s:06d}-{e:06d}.mp4"
        todo = [(s, e) for s, e in segs if not seg(s, e).exists()]
        have = (b - a) - sum(e - s for s, e in todo)
        if have:
            log(f"Resuming: {have} of {b - a} frames were rendered earlier.")
        log(f"Rendering {b - a} frames ({(b - a) / fps / 60:.1f} min of video) into {work}."
            " Ctrl+C stops; the same command resumes.")
        light = hdrlib.LightMeter(yuv=fmt == "p010") if args.hdr else None
        prog = Progress(b - a, have)
        try:
            for s, e in todo:
                if light is None:
                    render_frames(engine, range(s, e), seg(s, e), cfg, enc, lambda i, raw: prog.tick())
                    continue
                light.reset()
                render_frames(engine, range(s, e), seg(s, e), cfg, enc, lambda i, raw: (light.add(raw), prog.tick()))
                light.save(seg(s, e))
        finally:
            prog.end()
        if args.segments_only:
            engine.close()
            log(f"Segments only: {b - a} frames in {work} (no join).")
            return
        audio = None
        if args.audio in ("auto", "page"):
            if info["audio"]:
                audio = work / "audio-page.wav"
                if not audio.exists():
                    log("Rendering the page's soundtrack ...")
                    audio.write_bytes(engine.audio_wav(48000))
            elif args.audio == "page":
                sys.exit("--audio page: this page has no soundtrack (no audio() given to exposeRenderer)")
        elif args.audio != "none":
            audio = pathlib.Path(args.audio).expanduser().resolve()
            if not audio.is_file():
                sys.exit(f"--audio {args.audio}: no such file")
        peak = float((info.get("hdr") or {}).get("peakNits") or 1000)
    except KeyboardInterrupt:
        log("Stopped. Run the same command again to continue where it left off.")
        os._exit(130)
    except EngineTimeout as e:
        log(f"The browser stopped responding ({e}). Run the same command again to resume.")
        os._exit(1)
    except PageStop:
        engine.close()
        raise
    except Exception as e:
        log(f"Failed: {first_line(e)}")
        engine.close()
        raise
    engine.close()

    suffix = ".mkv" if args.hdr else ".mp4"
    default = f"{page.stem}-{oh}p{fps:g}" if ow * 9 == oh * 16 else f"{page.stem}-{ow}x{oh}-{fps:g}fps"
    if (a, b) != (0, n):
        default += f"-frames{a}-{b}"
    out = pathlib.Path(args.out).expanduser().resolve() if args.out else pathlib.Path.cwd() / (default + suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    log("Joining the segments" + (" and adding the soundtrack" if audio else "") + " ...")
    if args.hdr:
        lv = hdrlib.assemble([seg(s, e) for s, e in segs], audio, out, args.hdr_lut, cfg, work, peak,
                             audio_offset=a / fps, log=log, consume=args.consume_segments, expect_frames=b - a)
        log(f"HDR10: MaxCLL {lv['maxcll']} nits, MaxFALL {lv['maxfall']} nits; checked: the HDR10 metadata is there")
    else:
        assemble_sdr([seg(s, e) for s, e in segs], audio, out, fps, b - a, work, audio_offset=a / fps)
    log(f"saved {out} ({out.stat().st_size / 1e6:.1f} MB)")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


def probe(argv: list):
    """python -m renderfarm probe <page>: which browser profile gets hardware WebGL here, and what the page says of
    itself (frames, fps, soundtrack). One JSON line; exit 1 without hardware WebGL when --require-gpu."""
    ap = argparse.ArgumentParser(prog="python -m renderfarm probe")
    ap.add_argument("page")
    ap.add_argument("--root")
    ap.add_argument("--hdr", action="store_true")
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--require-gpu", action="store_true")
    args = ap.parse_args(argv)
    page = pathlib.Path(args.page).expanduser().resolve()
    root = pathlib.Path(args.root).expanduser().resolve() if args.root else default_root(page)
    cfg = dict(width=256, height=144, format="p010" if args.hdr else "nv12", fps=None, params=args.param)
    prof, info = pick_profile(page, root, cfg)
    if prof is None:
        eng = Engine(page, root, cfg, CPU_PROFILE)
        try:
            info = eng.open()
        finally:
            eng.close()
    out = {"profile": prof["name"] if prof else None, "gl": info["gl"], "frames": info["frames"], "fps": info["fps"],
           "audio": info["audio"], "hardware": prof is not None}
    print(json.dumps(out), flush=True)
    if args.require_gpu and prof is None:
        sys.exit(1)
