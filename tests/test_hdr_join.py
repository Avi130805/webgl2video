"""The HDR10 join: segments joined by stream copy into an MKV with the soundtrack (and a LUT), then the HDR10 metadata
written in place by MKVToolNix's mkvpropedit, then the file checked as YouTube needs it.

With MKVToolNix installed, the whole master is made and must pass its check. Without it, a stand-in mkvpropedit (which
writes nothing) lets everything else run, and the check must then find exactly the missing metadata and nothing else.
"""

import os
import shutil
import stat
import unittest

from common import PAGE, ROOT, have_browser, have_ffmpeg, rf, tmpdir


@unittest.skipUnless(have_browser() and have_ffmpeg(), "needs playwright (with its Chromium), numpy and ffmpeg")
class HdrJoin(unittest.TestCase):
    def test_join(self):
        d = tmpdir(self)
        code, out = rf("render", PAGE, "--hdr", "--size", "320x180", "--segment", "60", "--encoder", "x265", "--preset",
                       "ultrafast", "--frames", "0:180", "--work", d / "w", "--segments-only")
        self.assertEqual(code, 0, out)
        (d / "lut.cube").write_text("LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1)))
        env = {}
        real = shutil.which("mkvpropedit")
        if not real:
            fake = d / "bin" / "mkvpropedit"
            fake.parent.mkdir()
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = {"PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}"}
        code, out = rf("render", PAGE, "--hdr", "--size", "320x180", "--segment", "60", "--encoder", "x265", "--preset",
                       "ultrafast", "--frames", "0:180", "--work", d / "w", "--out", d / "m.mkv", "--hdr-lut", d / "lut.cube",
                       "--keep", env=env)
        if real:
            self.assertEqual(code, 0, out)
            self.assertIn("HDR10: MaxCLL", out)
            return
        self.assertNotEqual(code, 0)
        problems = [l.strip() for l in out.rsplit("the HDR10 master is not what YouTube needs:", 1)[1].splitlines() if l.strip()]
        self.assertEqual(problems, ["no mastering display metadata (ST 2086)", "MaxCLL/MaxFALL missing or out of range (None)"],
                         out)


if __name__ == "__main__":
    unittest.main()
