"""python -m webgl2video exact <page>: is the page's GPU conversion exact on this GPU?

The same frames are rendered twice through the page: as plain RGB (8-bit RGBA; with --hdr, 10-bit RGB) and in the
encoder's own form made on the GPU (NV12; with --hdr, P010 and the light levels). A float64 numpy reference of the same
conversion (BT.709 or BT.2020 limited range, 4:2:0 with the chroma sited left) is made from the RGB frames and compared
with the GPU's. Exact means: no sample off by more than one code, and those off by one a negligible share (rounding at
.5 in float32). Prints one line per frame and plane; exits 0 when exact, 1 when not. A cloud render runs it first on
the pod's GPU and falls back to ffmpeg's conversion (--convert cpu) when it fails.
"""

import argparse
import pathlib
import struct
import sys

from .browser import CPU_PROFILE, Engine, default_root, pick_profile
from .hdr import NITS
from .util import log

MAX_OFF_BY_ONE = 0.01  # percent of a plane's samples


def code(x, top):
    import numpy as np
    return np.clip(np.floor(x + 0.5), 0, top).astype(np.int32)


def chroma_left(rgb):
    """4:2:0 with the chroma sited left: the [1 2 1] / 4 filter across each even column (edges clamped), the mean of
    the two rows."""
    import numpy as np
    pad = np.concatenate([rgb[:, :1], rgb, rgb[:, -1:]], 1)
    hf = 0.25 * pad[:, 0:-2:2] + 0.5 * pad[:, 1:-1:2] + 0.25 * pad[:, 2::2]
    return 0.5 * (hf[0::2] + hf[1::2])


def nv12_reference(rgba: bytes, w: int, h: int):
    """BT.709 limited range from 8-bit RGBA (top row first): Y, Cb, Cr as int arrays."""
    import numpy as np
    K = np.array([0.2126, 0.7152, 0.0722])
    rgb = np.frombuffer(rgba, np.uint8).reshape(h, w, 4)[..., :3] / 255.0
    c = chroma_left(rgb)
    cl = c @ K
    return (code(16 + 219 * (rgb @ K), 255), code(128 + 224 * (c[..., 2] - cl) / 1.8556, 255),
            code(128 + 224 * (c[..., 0] - cl) / 1.5748, 255))


def nv12_planes(buf: bytes, w: int, h: int):
    import numpy as np
    a = np.frombuffer(buf, np.uint8)
    if a.size != w * h * 3 // 2:
        raise ValueError(f"an NV12 frame of {a.size} bytes, not {w * h * 3 // 2}")
    uv = a[w * h:].reshape(h // 2, w // 2, 2).astype(np.int32)
    return a[:w * h].reshape(h, w).astype(np.int32), uv[..., 0], uv[..., 1]


def rgb10(x2bgr10: bytes, w: int, h: int):
    """10-bit codes (h, w, 3) from x2bgr10le words."""
    import numpy as np
    v = np.frombuffer(x2bgr10, "<u4").reshape(h, w)
    return np.stack([v & 1023, (v >> 10) & 1023, (v >> 20) & 1023], -1).astype(np.int32)


def p010_reference(x2bgr10: bytes, w: int, h: int):
    """BT.2020 non-constant luminance, limited range, from 10-bit RGB: Y, Cb, Cr, and the light levels (the top code
    of every pixel's brightest channel, and the frame mean of that channel in nits)."""
    import numpy as np
    K = np.array([0.2627, 0.6780, 0.0593])
    c10 = rgb10(x2bgr10, w, h)
    rgb = c10 / 1023.0
    c = chroma_left(rgb)
    cl = c @ K
    m = c10.max(-1)
    return (code(64 + 876 * (rgb @ K), 1023), code(512 + 896 * (c[..., 2] - cl) / 1.8814, 1023),
            code(512 + 896 * (c[..., 0] - cl) / 1.4746, 1023), int(m.max()), float(np.array(NITS)[m].mean()))


def p010_planes(buf: bytes, w: int, h: int):
    import numpy as np
    n = w * h * 3
    if len(buf) != n + 16:
        raise ValueError(f"a P010 frame of {len(buf)} bytes, not {n + 16}")
    a = np.frombuffer(buf, "<u2", count=n // 2).astype(np.int32) >> 6
    uv = a[w * h:].reshape(h // 2, w // 2, 2)
    top, _, fall = struct.unpack_from("<IId", buf, n)
    return a[:w * h].reshape(h, w), uv[..., 0], uv[..., 1], top, fall


def compare(name: str, ref, got) -> bool:
    import numpy as np
    d = np.abs(ref - got)
    one, more = 100 * float((d == 1).mean()), 100 * float((d > 1).mean())
    print(f"{name}: max |d| {int(d.max())}, off by 1: {one:.4f}%, more: {more:.5f}%", flush=True)
    return int(d.max()) <= 1 and one < MAX_OFF_BY_ONE and more == 0


def grab(page, root, cfg: dict, profile, frames: list, require_hw: bool) -> dict:
    e = Engine(page, root, cfg, profile or CPU_PROFILE, require_hw=require_hw)
    try:
        info = e.open()
        bad = [i for i in frames if not 0 <= i < info["frames"]]
        if bad:
            sys.exit(f"frames {bad} are outside the page's 0..{info['frames'] - 1}")
        return {i: e.frame_raw(i) for i in frames}
    finally:
        e.close()


def main(argv: list):
    ap = argparse.ArgumentParser(prog="python -m webgl2video exact", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page")
    ap.add_argument("--root")
    ap.add_argument("--size", default="1280x720", help="WxH (default 1280x720; a pod checks at the render size)")
    ap.add_argument("--frames", default="0,1,2", help="frame indices, comma-separated")
    ap.add_argument("--hdr", action="store_true", help="P010 and the light levels against 10-bit RGB")
    ap.add_argument("--param", action="append", default=[], metavar="K=V")
    ap.add_argument("--cpu", action="store_true", help="SwiftShader (the reference is the same; a check of the shaders)")
    ap.add_argument("--require-gpu", action="store_true")
    args = ap.parse_args(argv)
    page = pathlib.Path(args.page).expanduser().resolve()
    root = pathlib.Path(args.root).expanduser().resolve() if args.root else default_root(page)
    w, h = (int(x) for x in args.size.lower().split("x"))
    frames = [int(x) for x in args.frames.split(",") if x.strip()]
    plain, gpu = ("x2bgr10", "p010") if args.hdr else ("rgba", "nv12")
    cfg = dict(width=w, height=h, out_width=w, out_height=h, fps=None, params=args.param)
    profile = None
    if not args.cpu:
        profile, info = pick_profile(page, root, dict(cfg, format=gpu))
        if profile is None and args.require_gpu:
            sys.exit("--require-gpu: no hardware WebGL here")
        log(f"WebGL: {info['gl'] if profile else 'SwiftShader (CPU)'}")
    a = grab(page, root, dict(cfg, format=plain), profile, frames, profile is not None)
    b = grab(page, root, dict(cfg, format=gpu), profile, frames, profile is not None)
    ok = True
    for i in frames:
        if args.hdr:
            ry, rcb, rcr, rtop, rfall = p010_reference(a[i], w, h)
            gy, gcb, gcr, gtop, gfall = p010_planes(b[i], w, h)
        else:
            ry, rcb, rcr = nv12_reference(a[i], w, h)
            gy, gcb, gcr = nv12_planes(b[i], w, h)
        for name, r, g in (("Y ", ry, gy), ("Cb", rcb, gcb), ("Cr", rcr, gcr)):
            ok &= compare(f"frame {i} {name}", r, g)
        if args.hdr:
            rel = abs(gfall - rfall) / max(rfall, 1e-9)
            print(f"frame {i} light: top code {gtop} (reference {rtop}), MaxFALL {gfall:.4f} nits (reference {rfall:.4f},"
                  f" off by {100 * rel:.5f}%)", flush=True)
            ok &= gtop == rtop and rel < 1e-4
    print("EXACT: the GPU conversion matches the reference" if ok else "NOT EXACT: use --convert cpu (ffmpeg converts)",
          flush=True)
    sys.exit(0 if ok else 1)
