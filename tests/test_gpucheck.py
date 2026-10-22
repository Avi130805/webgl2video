"""gpucheck: it names the renderer, and fails loudly (exit 1) when Chromium only has a software renderer."""

import contextlib
import io
import unittest

from common import have_browser

import webgl2video.gpucheck as gc
from webgl2video.browser import CPU_PROFILE


@unittest.skipUnless(have_browser(), "needs playwright (with its Chromium)")
class GpuCheck(unittest.TestCase):
    def test_software_renderer_fails_loudly(self):
        saved = list(gc.GPU_PROFILES)
        gc.GPU_PROFILES[:] = [CPU_PROFILE]  # as on a server where Chromium cannot reach the GPU
        self.addCleanup(gc.GPU_PROFILES.__setitem__, slice(None), saved)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            gc.main([])
        self.assertIn("SwiftShader", out.getvalue())
        self.assertIn("NO ", out.getvalue())
        self.assertIn("hardware WebGL: NO", str(stop.exception.code))


if __name__ == "__main__":
    unittest.main()
