"""HDR10: light levels, and the master packaged the way YouTube asks (support.google.com/youtube/answer/7126552).

PQ, Rec. 2020 primaries and matrix, 10 bits, HEVC, in an MKV with the SMPTE ST 2086 mastering display, MaxCLL and
MaxFALL, and optionally a .cube LUT attached, which YouTube uses to make the SDR version
(github.com/YouTubeHDR/hdr_metadata).

  LightMeter       MaxCLL and MaxFALL (CTA-861.3) of every frame as it passes: read from the 16 bytes the page's GPU
                   meter appends to each P010 frame, or measured from 10-bit RGB frames with numpy. Saved beside each
                   segment (<segment>.light.json), so a resumed render, or a join on another machine, still knows them
  assemble(...)    the cheap checks first (the segments alike and HDR10, their light levels, the frame count, the disk),
                   then the segments joined by stream copy with the soundtrack (and the LUT), then the HDR10 metadata
                   written in place by mkvpropedit, then check(). The join names the track with a 256-character
                   placeholder, which the same mkvpropedit pass deletes, so the metadata fits where Tracks is, ahead of
                   the frames, with no copy of the file. The stream's own SEI messages are dropped in the join (the
                   light levels are known only at the end). consume=True, for a small disk: each segment is deleted
                   once ffmpeg has opened the next, so the peak is the segments plus one. A marker left after the join
                   lets a rerun finish the master without the deleted segments
  check(path)      the file read back: HEVC Main 10, yuv420p10le, BT.2020 / PQ / BT.2020nc, video range, the ST 2086
                   values, MaxCLL and MaxFALL, the LUT if one was given, Tracks before the first Cluster, every frame

Needs MKVToolNix (mkvpropedit) for the master; numpy only to meter 10-bit RGB frames on the CPU.
"""

import concurrent.futures
import json
import math
import os
import pathlib
import shutil
import struct
import subprocess
import threading
import time

from .util import ffmpeg_bin, ffprobe_bin

# The mastering display (ST 2086): P3-D65 primaries, as YouTube's metadata tool writes them; the peak is the page's.
DISPLAY = dict(min_luminance=0.0001, primaries=dict(r=(0.68, 0.32), g=(0.265, 0.69), b=(0.15, 0.06), white=(0.3127, 0.329)))

# SMPTE ST 2084
M1, M2 = 2610 / 16384, 2523 / 4096 * 128
C1, C2, C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32


def pq_nits(code: float) -> float:
    """A PQ code value (0..1) as nits."""
    p = max(0.0, min(1.0, code)) ** (1 / M2)
    return 10000 * (max(p - C1, 0) / (C2 - C3 * p)) ** (1 / M1)


NITS = [pq_nits(c / 1023) for c in range(1024)]  # each 10-bit code's luminance


class ToolError(RuntimeError):
    """A tool that failed: its name, the command, and its own output (what a log should show)."""

    def __init__(self, cmd: list, output: str):
        self.tool, self.output = pathlib.Path(str(cmd[0])).name, output
        super().__init__(f"{self.tool} failed:\n" + " ".join(map(str, cmd)) + "\n" + output[-4000:])


def run(cmd: list) -> str:
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise ToolError(cmd, r.stdout)
    return r.stdout


def need(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        raise RuntimeError(f"{tool} not found: the HDR10 master needs MKVToolNix (apt install mkvtoolnix, brew install mkvtoolnix)")
    return exe


# ---- light levels ----------------------------------------------------------------------------------------------------

class LightMeter:
    """MaxCLL and MaxFALL (CTA-861.3): the brightest channel of the brightest pixel, and the highest frame average of
    each pixel's brightest channel, in nits. yuv=True: the page metered each frame on the GPU and sent the two levels
    in the frame's last 16 bytes, so add() only reads them; else add() measures 10-bit RGB frames on a worker thread."""

    def __init__(self, yuv: bool = False):
        self.yuv = yuv
        self.pending = []
        self.pool = None
        if not yuv:
            import numpy as np
            self.np, self.nits = np, np.array(NITS)
            self.pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="light")
        self.reset()

    def reset(self):
        self.top, self.fall, self.frames = 0, 0.0, 0

    def _measure(self, raw: bytes):
        np = self.np
        v = np.frombuffer(raw, dtype="<u4")
        m = np.maximum.reduce([v & 1023, (v >> 10) & 1023, (v >> 20) & 1023])  # each pixel's brightest channel
        h = np.bincount(m, minlength=1024)  # a histogram of codes: no float copy of the frame
        return int(np.flatnonzero(h)[-1]), float(h @ self.nits) / m.size

    def _take(self, fut):
        top, fall = fut.result()
        self.top, self.fall, self.frames = max(self.top, top), max(self.fall, fall), self.frames + 1

    def add(self, raw: bytes):
        if self.yuv:
            top, _, fall = struct.unpack_from("<IId", raw, len(raw) - 16)
            self.top, self.fall, self.frames = max(self.top, top), max(self.fall, fall), self.frames + 1
            return
        self.pending.append(self.pool.submit(self._measure, raw))
        while len(self.pending) > 3:  # a few frames in flight at most (33 MB each at 4K)
            self._take(self.pending.pop(0))

    def levels(self) -> dict:
        while self.pending:
            self._take(self.pending.pop(0))
        return {"maxcll": NITS[min(self.top, 1023)], "maxfall": self.fall, "frames": self.frames}

    def save(self, segment) -> dict:
        """The levels of the frames added since the last save, written to <segment>.light.json."""
        lv = self.levels()
        tmp = light_file(segment).with_suffix(".tmp")
        tmp.write_text(json.dumps(lv) + "\n")
        os.replace(tmp, light_file(segment))
        self.reset()
        return lv


def light_file(segment) -> pathlib.Path:
    p = pathlib.Path(segment)
    return p.with_name(p.stem + ".light.json")


def measure_file(path, w: int, h: int) -> dict:
    """The levels of an encoded segment, decoded (for a segment whose .light.json is missing)."""
    import numpy as np
    nits = np.array(NITS)
    proc = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-i", str(path),
                             "-vf", "scale=in_color_matrix=bt2020:in_range=tv,format=gbrp10le", "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE)
    top, fall, frames, size = 0, 0.0, 0, w * h * 2 * 3
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        m = np.frombuffer(buf, dtype="<u2").reshape(3, h * w).max(0)
        top, fall, frames = max(top, int(m.max())), max(fall, float(nits[m].mean())), frames + 1
    proc.wait()
    return {"maxcll": NITS[min(top, 1023)], "maxfall": fall, "frames": frames}


# ---- the master ------------------------------------------------------------------------------------------------------

def _open_files(pid: int) -> set:
    """The files a process has open (Linux: /proc; macOS: lsof)."""
    fds = pathlib.Path(f"/proc/{pid}/fd")
    if fds.exists():
        out = set()
        for fd in list(fds.iterdir()):
            try:
                out.add(os.path.realpath(os.readlink(fd)))
            except OSError:
                pass
        return out
    r = subprocess.run(["lsof", "-p", str(pid), "-Fn"], capture_output=True, text=True)
    return {os.path.realpath(line[1:]) for line in r.stdout.splitlines() if line.startswith("n")}


def _join(cmd: list, segments: list, consume: bool, log) -> list:
    """Run the concat join; with `consume`, delete each segment once ffmpeg has opened a later one. Returns the
    segments deleted."""
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    paths = [os.path.realpath(p) for p in segments]
    index = {p: k for k, p in enumerate(paths)}
    gone = []

    def watch():
        while proc.poll() is None:
            seen = [index[p] for p in _open_files(proc.pid) if p in index]
            for j in range(max(seen) if seen else 0):
                if paths[j] not in gone and os.path.exists(paths[j]):
                    os.remove(paths[j])
                    gone.append(paths[j])
            time.sleep(0.25)

    watcher = threading.Thread(target=watch, daemon=True) if consume else None
    if watcher:
        watcher.start()
    out, _ = proc.communicate()
    if watcher:
        watcher.join()
    if proc.returncode != 0:
        lost = f"\n{len(gone)} segments were already deleted: the same command renders them again" if gone else ""
        raise RuntimeError("ffmpeg failed:\n" + " ".join(map(str, cmd)) + "\n" + out[-4000:] + lost)
    if gone:
        log(f"  the join read and deleted {len(gone)} of {len(segments)} segments")
    return gone


PLACEHOLDER = "reserved for the HDR10 metadata".ljust(256, ".")  # the join's track name: room in Tracks for the metadata


def marker(tmp_dir, master) -> pathlib.Path:
    return pathlib.Path(tmp_dir) / f"joined-{pathlib.Path(master).stem}.json"


def finished_join(tmp_dir, master):
    """What a successful join left for `master` (its light levels) while the master is still to be finished, or None.
    A rerun then redoes only the metadata and the check, never the segments a consuming join deleted."""
    m = marker(tmp_dir, master)
    if not m.exists():
        return None
    x = json.loads(m.read_text())
    return x if pathlib.Path(x["part"]).exists() else None


def preflight(segments: list, lut, cfg: dict, out_dir, consume: bool, expect_frames, log) -> dict:
    """The cheap checks, before the join consumes anything: the LUT, every segment there and alike, the first one's
    stream what YouTube needs, the light levels of every segment, the frame count, the disk. Returns the levels."""
    if lut and (pathlib.Path(lut).suffix != ".cube" or not pathlib.Path(lut).exists()):
        raise ValueError(f"the LUT {lut}: YouTube reads it only from a file named *.cube, and it must exist")
    missing = [pathlib.Path(s).name for s in segments if not pathlib.Path(s).exists()]
    if missing:
        raise RuntimeError(f"{len(missing)} segments are missing ({', '.join(missing[:4])} ...): run the same command to render them")
    same_parameter_sets(segments)
    v = json.loads(run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", segments[0]]))["streams"][0]
    want = {"codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le", "color_range": "tv",
            "color_space": "bt2020nc", "color_transfer": "smpte2084", "color_primaries": "bt2020"}
    bad = [f"{k} is {v.get(k)!r}, not {x!r}" for k, x in want.items() if v.get(k) != x]
    if bad:
        raise RuntimeError("the segments are not HDR10 video: " + "; ".join(bad))
    lv = {"maxcll": 0.0, "maxfall": 0.0, "frames": 0}
    for s in segments:  # the light levels, measured as the frames rendered (decoded again where they were not saved)
        f = light_file(s)
        if f.exists():
            x = json.loads(f.read_text())
        else:
            log(f"  measuring {pathlib.Path(s).name} (its light levels were not saved)")
            x = measure_file(s, cfg.get("out_width", cfg["width"]), cfg.get("out_height", cfg["height"]))
        lv = {"maxcll": max(lv["maxcll"], x["maxcll"]), "maxfall": max(lv["maxfall"], x["maxfall"]), "frames": lv["frames"] + x["frames"]}
    if expect_frames and lv["frames"] != expect_frames:
        raise RuntimeError(f"the segments hold {lv['frames']} frames, not {expect_frames}")
    sizes = [pathlib.Path(s).stat().st_size for s in segments]
    need_bytes = (max(sizes) if consume else sum(sizes)) + 512 * 1024 * 1024
    free = shutil.disk_usage(out_dir).free
    if free < need_bytes:
        raise RuntimeError(f"{free / 1e9:.1f} GB free where the master goes, and the join needs about {need_bytes / 1e9:.1f} GB"
                           + ("" if consume else " (with --consume-segments, about the largest segment plus 0.5 GB)"))
    return lv


def assemble(segments: list, audio, master, lut, cfg: dict, tmp_dir, peak: float, audio_offset: float = 0.0,
             log=print, consume: bool = False, expect_frames: int = None) -> dict:
    """Join the segments into `master` (.mkv) with the soundtrack (and the LUT), write the HDR10 metadata in place,
    check it. Returns the light levels written. After a successful join a marker in tmp_dir lets a rerun finish the
    master from the joined file (finished_join)."""
    master, tmp_dir = pathlib.Path(master), pathlib.Path(tmp_dir)
    mkvpropedit = need("mkvpropedit")
    part = master.with_name(master.stem + ".part.mkv")
    lst = tmp_dir / f"segments-{master.stem}.txt"
    done = finished_join(tmp_dir, master)
    if done:  # the join finished earlier: only the metadata and the check
        lv = done["levels"]
    else:
        master.parent.mkdir(parents=True, exist_ok=True)
        lv = preflight(segments, lut, cfg, master.parent, consume, expect_frames, log)
        lst.write_text("".join("file '{}'\n".format(str(pathlib.Path(p).resolve()).replace("'", "'\\''")) for p in segments))
        sound = ([*(["-ss", f"{audio_offset:.3f}"] if audio_offset else []), "-i", str(audio)] if audio else [])
        attach = (["-attach", str(lut), "-metadata:s:t", "mimetype=application/x-cube",
                   "-metadata:s:t", f"filename={pathlib.Path(lut).name}"] if lut else [])
        # the join: a stream copy, the stream's own SEI dropped, the LUT attached, and a placeholder name that keeps
        # room in Tracks for the metadata (so mkvpropedit writes it in place, ahead of the frames)
        _join([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, *sound,
               "-map", "0:v:0", *(["-map", "1:a:0", "-c:a", "aac", "-b:a", "320k", "-ar", "48000"] if audio else []),
               "-c:v", "copy", "-bsf:v", "filter_units=remove_types=39|40", "-metadata:s:v:0", f"title={PLACEHOLDER}",
               "-t", f"{lv['frames'] / float(cfg['fps']):.6f}", *attach, part],
              segments, consume, log)
        marker(tmp_dir, master).write_text(json.dumps({"part": str(part), "levels": lv}) + "\n")
    maxcll = min(peak, math.ceil(lv["maxcll"]))
    maxfall = max(1, min(maxcll, math.ceil(lv["maxfall"])))
    p = DISPLAY["primaries"]
    props = {
        "color-matrix-coefficients": 9, "color-range": 1, "color-transfer-characteristics": 16, "color-primaries": 9,
        "max-content-light": maxcll, "max-frame-light": maxfall,
        "max-luminance": peak, "min-luminance": DISPLAY["min_luminance"],
        "chromaticity-coordinates-red-x": p["r"][0], "chromaticity-coordinates-red-y": p["r"][1],
        "chromaticity-coordinates-green-x": p["g"][0], "chromaticity-coordinates-green-y": p["g"][1],
        "chromaticity-coordinates-blue-x": p["b"][0], "chromaticity-coordinates-blue-y": p["b"][1],
        "white-coordinates-x": p["white"][0], "white-coordinates-y": p["white"][1],
    }
    # the placeholder goes and the metadata takes its room, written in place
    run([mkvpropedit, part, "--edit", "track:v1", "--delete", "name",
         *[a for k, v in props.items() for a in ("--set", f"{k}={v}")]])
    problems = check(part, peak, expect_frames=lv["frames"] or None, lut=bool(lut), audio=bool(audio))
    if problems:
        raise RuntimeError("the HDR10 master is not what YouTube needs:\n  " + "\n  ".join(problems))
    part.replace(master)
    marker(tmp_dir, master).unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    return {"maxcll": maxcll, "maxfall": maxfall, "frames": lv["frames"]}


def same_parameter_sets(segments: list):
    """Stop unless every segment carries the same parameter sets (the same encoder settings): a stream copy keeps the
    first segment's for the whole film."""
    groups = {}
    for s in segments:
        h = run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_data_hash", "md5", "-show_entries",
                 "stream=extradata_hash", "-of", "csv=p=0", s]).strip()
        groups.setdefault(h, []).append(pathlib.Path(s).name)
    if len(groups) > 1:
        lines = [f"{h or '(none)'}: {', '.join(v[:6])}{' ...' if len(v) > 6 else ''} ({len(v)})" for h, v in groups.items()]
        raise RuntimeError("the segments were encoded with different settings (their parameter sets differ), so they "
                           "cannot be joined without re-encoding. Delete the odd ones and render them again:\n  " + "\n  ".join(lines))


# the Matroska elements check() looks for at the top of the Segment
_TRACKS, _CLUSTER, _SEGMENT = 0x1654AE6B, 0x1F43B675, 0x18538067


def _vint(f, keep_marker: bool):
    b = f.read(1)
    if not b:
        return None
    n, mask = 1, 0x80
    while n <= 8 and not (b[0] & mask):
        n, mask = n + 1, mask >> 1
    v = b[0] if keep_marker else b[0] & (mask - 1)
    for c in f.read(n - 1):
        v = (v << 8) | c
    return v


def tracks_first(path) -> bool:
    """Does the Segment's Tracks element come before its first Cluster (the header before the frames)?"""
    with open(path, "rb") as f:
        _vint(f, True)
        f.seek(_vint(f, False), 1)  # the EBML header
        if _vint(f, True) != _SEGMENT:
            return False
        _vint(f, False)
        while True:
            eid = _vint(f, True)
            if eid is None or eid == _CLUSTER:
                return False
            if eid == _TRACKS:
                return True
            f.seek(_vint(f, False), 1)


def _ratio(x) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    a, _, b = str(x).partition("/")
    return float(a) / float(b or 1)


def check(path, peak: float, expect_frames: int = None, lut: bool = False, audio: bool = True) -> list:
    """What YouTube needs of an HDR10 upload, read back from the file. Returns the problems (an empty list: ready)."""
    info = json.loads(run([ffprobe_bin(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        return ["no video stream"]
    bad = []
    want = {"codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le", "color_range": "tv",
            "color_space": "bt2020nc", "color_transfer": "smpte2084", "color_primaries": "bt2020"}
    for k, x in want.items():
        if v.get(k) != x:
            bad.append(f"{k} is {v.get(k)!r}, not {x!r}")
    side = {d.get("side_data_type"): d for d in v.get("side_data_list", [])}
    md = side.get("Mastering display metadata")
    if not md:
        bad.append("no mastering display metadata (ST 2086)")
    else:
        p = DISPLAY["primaries"]
        for key, val in (("red_x", p["r"][0]), ("red_y", p["r"][1]), ("green_x", p["g"][0]), ("green_y", p["g"][1]),
                         ("blue_x", p["b"][0]), ("blue_y", p["b"][1]), ("white_point_x", p["white"][0]),
                         ("white_point_y", p["white"][1]), ("max_luminance", peak), ("min_luminance", DISPLAY["min_luminance"])):
            if key not in md or abs(_ratio(md[key]) - val) > max(1e-4, val * 1e-3):
                bad.append(f"mastering display {key} is {md.get(key)}, not {val}")
    cl = side.get("Content light level metadata")
    if not cl or not (0 < int(cl.get("max_content", 0)) <= peak) or not (0 < int(cl.get("max_average", 0)) <= int(cl.get("max_content", 0))):
        bad.append(f"MaxCLL/MaxFALL missing or out of range ({cl})")
    if lut and not [s for s in info["streams"] if s.get("codec_type") == "attachment"
                    and s.get("tags", {}).get("mimetype") == "application/x-cube"
                    and str(s.get("tags", {}).get("filename", "")).endswith(".cube")]:
        bad.append("no .cube LUT attached (application/x-cube)")
    if audio and not any(s.get("codec_type") == "audio" for s in info["streams"]):
        bad.append("no soundtrack")
    if str(path).endswith(".mkv") and not tracks_first(path):
        bad.append("Tracks (the header with the HDR10 metadata) comes after the frames: remux it with mkvmerge")
    if expect_frames:  # every frame there (packets counted, nothing decoded)
        n = json.loads(run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                            "stream=nb_read_packets", "-of", "json", path]))["streams"][0].get("nb_read_packets")
        if str(n) != str(expect_frames):
            bad.append(f"{n} frames, not {expect_frames}")
    return bad
