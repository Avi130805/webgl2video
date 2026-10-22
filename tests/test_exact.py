"""The exactness test: the page's GPU conversion against a float64 reference, on this machine's WebGL.

NV12 (8-bit BT.709) from the same frames read back as RGBA, and P010 (10-bit BT.2020) with its light meter (MaxCLL,
MaxFALL) from the same frames read back as 10-bit RGB: no sample off by more than one code, those off by one under
0.01%, the light levels equal. Frames on both sides of the example's hard cut. Without a GPU it runs on SwiftShader
(the same shaders, the same reference). A cloud render runs the same check on the pod's GPU before it trusts the path.
"""

import unittest

from common import PAGE, have_browser, have_ffmpeg, rf

FRAMES = "0,119,120,300,599"


@unittest.skipUnless(have_browser(), "needs playwright (with its Chromium) and numpy")
class Exact(unittest.TestCase):
    def check(self, *extra):
        code, out = rf("exact", PAGE, "--size", "640x360", "--frames", FRAMES, *extra)
        self.assertEqual(code, 0, out)
        self.assertIn("EXACT: the GPU conversion matches the reference", out)
        return out

    def test_nv12(self):
        out = self.check()
        self.assertEqual(out.count(" Y : max |d|"), 5)

    def test_p010_and_light(self):
        out = self.check("--hdr")
        self.assertEqual(out.count(" light: top code"), 5)


@unittest.skipUnless(have_browser(), "needs playwright (with its Chromium) and numpy")
class CanvasMode(unittest.TestCase):
    """A page that draws on a canvas (any API) and lets the helper copy it gives the same frames as one that renders
    into the helper's target; a Canvas 2D page converts exactly too."""

    def grab(self, page, fmt, params, frames):
        from webgl2video.browser import CPU_PROFILE, Engine, pick_profile
        root = PAGE.parent.parent.parent
        cfg = dict(width=640, height=360, out_width=640, out_height=360, fps=None, params=params, format=fmt)
        prof, _ = pick_profile(page, root, cfg)
        e = Engine(page, root, cfg, prof or CPU_PROFILE)
        try:
            e.open()
            return [e.frame_raw(i) for i in frames]
        finally:
            e.close()

    def test_same_frames_as_the_target(self):
        for fmt in ("rgba", "nv12"):
            self.assertEqual(self.grab(PAGE, fmt, [], [0, 130, 400]), self.grab(PAGE, fmt, ["source=canvas"], [0, 130, 400]), fmt)

    def test_canvas2d_exact(self):
        code, out = rf("exact", PAGE.parent.parent / "canvas2d" / "index.html", "--size", "640x360", "--frames", "0,90,300")
        self.assertEqual(code, 0, out)
        self.assertIn("EXACT", out)


@unittest.skipUnless(have_browser() and have_ffmpeg(), "needs playwright, numpy and ffmpeg")
class AgainstFfmpeg(unittest.TestCase):
    """What ffmpeg's own conversion (the --convert cpu path) makes of the same RGB frames, against the reference: close
    (chroma sited left, as the GPU path sites it), not exact. Records the difference rather than hiding it."""

    def test_nv12_close(self):
        import pathlib
        import subprocess
        import numpy as np
        from webgl2video.browser import CPU_PROFILE, Engine, pick_profile
        from webgl2video.encode import LEFT
        from webgl2video.exact import nv12_planes, nv12_reference
        w, h = 320, 180
        cfg = dict(width=w, height=h, out_width=w, out_height=h, fps=None, params=[], format="rgba")
        prof, _ = pick_profile(PAGE, PAGE.parent.parent.parent, cfg)
        e = Engine(PAGE, PAGE.parent.parent.parent, cfg, prof or CPU_PROFILE)
        try:
            e.open()
            raw = e.frame_raw(300)
        finally:
            e.close()
        out = subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-i", "-",
                              "-vf", f"scale=out_color_matrix=bt709:out_range=tv:{LEFT},format=nv12", "-f", "rawvideo", "-"],
                             input=raw, capture_output=True, check=True).stdout
        # measured: luma within one code; chroma off by more than 2 codes on under 0.1% of samples (at sharp colour
        # edges, up to 5), a mean difference under 0.15 code. With swscale's default (centred) chroma: up to 7 codes,
        # on 10-20% of samples.
        for name, ref, got in zip(("Y", "Cb", "Cr"), nv12_reference(raw, w, h), nv12_planes(out, w, h)):
            d = np.abs(ref - got)
            if name == "Y":
                self.assertLessEqual(int(d.max()), 1, name)
            self.assertLess(float((d > 2).mean()), 0.005, name)
            self.assertLess(float(d.mean()), 0.25, name)


if __name__ == "__main__":
    unittest.main()
