"""End to end on this machine: the example page rendered, resumed, rendered in pieces, joined, checked and verified."""

import hashlib
import json
import pathlib
import re
import unittest

from common import PAGE, have_browser, have_ffmpeg, frames_in, rf, tmpdir

COMMON = ["--size", "320x180", "--segment", "60", "--encoder", "x264", "--preset", "ultrafast", "--param", "seconds=4"]


def md5s(work: pathlib.Path) -> dict:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(work.glob("seg_*.mp4"))}


@unittest.skipUnless(have_browser() and have_ffmpeg(), "needs playwright (with its Chromium), numpy and ffmpeg")
class Render(unittest.TestCase):
    def test_render_resume_pieces_join_qa_verify(self):
        d = tmpdir(self)
        # the whole film (240 frames at 60 fps) in one run, with the page's soundtrack
        code, out = rf("render", PAGE, *COMMON, "--work", d / "a", "--out", d / "a.mp4", "--keep")
        self.assertEqual(code, 0, out)
        self.assertEqual(frames_in(d / "a.mp4"), 240)
        self.assertTrue((d / "a" / "audio-page.wav").exists())
        # the same command again renders nothing
        code, out = rf("render", PAGE, *COMMON, "--work", d / "a", "--out", d / "a2.mp4", "--keep")
        self.assertEqual(code, 0, out)
        self.assertIn("Resuming: 240 of 240 frames", out)
        # pieces rendered apart (two processes, as two cloud workers) give the very same segments, byte for byte
        for piece in ("0:120", "120:240"):
            code, out = rf("render", PAGE, *COMMON, "--work", d / "b", "--frames", piece, "--segments-only")
            self.assertEqual(code, 0, out)
        self.assertEqual(md5s(d / "a"), md5s(d / "b"))
        # a work folder refuses segments of other settings
        code, out = rf("render", PAGE, *COMMON[:-2], "--param", "seconds=5", "--work", d / "a", "--segments-only")
        self.assertNotEqual(code, 0)
        self.assertIn("holds segments of another page or other settings", out)
        # the QA: the hard cut at 120 is found (and flagged unless it is declared), at the seam between two jobs too
        code, out = rf("qa", d / "b", 120, 240, d / "qa")
        self.assertEqual(code, 0, out)
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual([f["frame"] for f in res["flags"]], [120])
        code, out = rf("qa", d / "b", 120, 240, d / "qa", "--cuts", "120")
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((res["flags"], [c["frame"] for c in res["known_cuts"]]), ([], [120]))
        # the finished video: every frame, the soundtrack in sync with the page's
        code, out = rf("verify", d / "a.mp4", "--expect-frames", 240, "--audio", d / "a" / "audio-page.wav",
                       "--stills", d / "stills")
        self.assertEqual(code, 0, out)
        offsets = [float(x) for x in re.findall(r"SYNC at [\d.]+ s: offset ([+-][\d.]+) ms", out)]
        self.assertTrue(offsets and all(abs(x) <= 1 for x in offsets), out)
        self.assertEqual(len(list((d / "stills").glob("*.jpg"))), 12)
        code, out = rf("verify", d / "a.mp4", "--expect-frames", 241)
        self.assertNotEqual(code, 0)

    def test_hdr_segments_and_light(self):
        d = tmpdir(self)
        args = ["--hdr", "--size", "320x180", "--segment", "60", "--encoder", "x265", "--preset", "ultrafast",
                "--frames", "60:180", "--segments-only"]
        code, out = rf("render", PAGE, *args, "--work", d / "gpu")
        self.assertEqual(code, 0, out)
        code, out = rf("render", PAGE, *args, "--convert", "cpu", "--work", d / "cpu")
        self.assertEqual(code, 0, out)
        for name in ("seg_000060-000120.light.json", "seg_000120-000180.light.json"):
            gpu, cpu = (json.loads((d / k / name).read_text()) for k in ("gpu", "cpu"))
            self.assertEqual(gpu["frames"], 60)
            self.assertEqual(gpu["maxcll"], cpu["maxcll"])  # the brightest code, exactly
            self.assertAlmostEqual(gpu["maxfall"], cpu["maxfall"], delta=cpu["maxfall"] * 1e-4)
        probe = __import__("subprocess").run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=profile,pix_fmt,color_space,color_transfer,color_primaries",
             "-of", "json", str(d / "gpu" / "seg_000060-000120.mp4")], capture_output=True, text=True).stdout
        s = json.loads(probe)["streams"][0]
        self.assertEqual((s["profile"], s["pix_fmt"], s["color_transfer"], s["color_primaries"]),
                         ("Main 10", "yuv420p10le", "smpte2084", "bt2020"))


if __name__ == "__main__":
    unittest.main()
