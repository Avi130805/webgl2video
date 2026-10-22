"""The cloud side, without a cloud: the queue, the scaling test's arithmetic, the deploy request, the NVENC shim, the
launcher's bundle, and the whole pod job run on this machine (setup skipped, x264 instead of NVENC, two workers)."""

import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import time
import unittest

from common import PY, ROOT, have_browser, have_ffmpeg, tmpdir

RUNPOD = ROOT / "cloud" / "runpod"


def queue(frames, nmax, *extra):
    r = subprocess.run([PY, RUNPOD / "queue.py", str(frames), str(nmax), *extra], capture_output=True, text=True, check=True)
    return [tuple(map(int, l.split(":"))) for l in r.stdout.split()], r.stderr.strip()


class Queue(unittest.TestCase):
    def test_every_frame_once_test_pieces_then_heavy(self):
        jobs, stages = queue(36060, 11, "--job", "500", "--heavy", "9840:10800 18960:20760")
        self.assertEqual(stages, "STAGES 1 4 11")
        covered = sorted(f for a, b in jobs for f in (a, b))
        self.assertEqual(sorted(a for a, _ in jobs), sorted(set(a for a, _ in jobs)))
        self.assertEqual(sum(b - a for a, b in jobs), 36060)
        self.assertEqual((covered[0], covered[-1]), (0, 36060))
        self.assertTrue(all(a % 500 == 0 and (b - a == 500 or b == 36060) for a, b in jobs))
        heavy = lambda j: j[0] < 10800 and 9840 < j[1] or j[0] < 20760 and 18960 < j[1]
        self.assertFalse(any(heavy(j) for j in jobs[:16]))  # the scaling test renders ordinary pieces
        self.assertEqual(sum(map(heavy, jobs)), 8)
        self.assertTrue(all(heavy(j) for j in jobs[16:16 + 8]))  # then the heavy ranges, first
        spread = sorted(a for a, _ in jobs[:16])
        self.assertGreater(spread[-1] - spread[0], 30000)  # across the film

    def test_small_films_and_few_workers(self):
        self.assertEqual(queue(240, 2, "--job", "60")[1], "STAGES 1 2")
        self.assertEqual(queue(240, 1, "--job", "60")[1], "STAGES 1")
        jobs, stages = queue(100, 16, "--job", "60")
        self.assertEqual((jobs, stages), ([(0, 60), (60, 100)], "STAGES 1 1"))


class Ladder(unittest.TestCase):
    def test_stage_and_projection(self):
        w = tmpdir(self)
        (w / "logs").mkdir()
        for a, b, t0 in ((0, 500, 0), (500, 1000, 5)):  # two jobs at 10 frames/s each, overlapping for 45 s
            lines = [f"12:00:{t0 + s:02d}   {10 * s}/500 frames · 0.10 s per frame · about 1 min left" for s in range(0, 51, 5)]
            (w / "logs" / f"job-{a}-{b}.log").write_text("\n".join(lines) + "\n")
        r = subprocess.run([PY, RUNPOD / "ladder.py", "stage", w, "s2-w2", "0", "60", "0:500", "500:1000"],
                           capture_output=True, text=True, check=True, env={**os.environ, "LADDER_MIN_WINDOW": "20"})
        self.assertIn("steady 20.00 frames/s", r.stdout)
        r = subprocess.run([PY, RUNPOD / "ladder.py", "project", w, "36000", "1000", "0.29", str(int(time.time())), "72", "2",
                            "--budget", "0.40"], capture_output=True, text=True, check=True)
        lines = r.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith("PROJECTION 2 workers: 20.00 frames/s steady"), r.stdout)
        self.assertEqual(lines[-1], "AUTO go 2")  # 29 min + 35 page loads + 12 = about 76 min, $0.37
        r = subprocess.run([PY, RUNPOD / "ladder.py", "project", w, "36000", "1000", "0.29", str(int(time.time())), "72", "2",
                            "--budget", "0.30"], capture_output=True, text=True, check=True)
        self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("AUTO stop"))


class Deploy(unittest.TestCase):
    def test_request_has_cores_public_ip_and_hides_values(self):
        r = subprocess.run([PY, RUNPOD / "deploy.py", "--gpu", "NVIDIA RTX 4000 Ada Generation", "--min-vcpu", "16",
                            "--volume-gb", "40", "--env", json.dumps({"YT_REFRESH_TOKEN": "{{ RUNPOD_SECRET_x }}"}),
                            "--dry-run"], capture_output=True, text=True, check=True)
        q = r.stdout
        for want in ("podFindAndDeployOnDemand", "minVcpuCount: 16", "supportPublicIp: true", 'volumeMountPath: "/workspace"',
                     'gpuTypeId: "NVIDIA RTX 4000 Ada Generation"', 'key: "YT_REFRESH_TOKEN", value: "..."'):
            self.assertIn(want, q)
        self.assertNotIn("RUNPOD_SECRET", q)


@unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
class NvencShim(unittest.TestCase):
    def test_mock_driver(self):
        d = tmpdir(self)
        subprocess.run(["cc", "-DNVENC_FILTER_TEST", "-o", d / "t", ROOT / "nvenc-gpu-filter" / "nvenc-gpu-filter.c", "-lpthread"],
                       check=True, capture_output=True)
        r = subprocess.run([d / "t"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("all passed", r.stdout)


@unittest.skipUnless(have_browser() and have_ffmpeg(), "needs playwright (with its Chromium), numpy and ffmpeg")
class PodJobHere(unittest.TestCase):
    """launch.sh's dry run builds the bundle; job.sh runs it as a pod would, with the setup skipped: preflight, the
    exactness check, the queue, a 1/2-worker scaling test, the decision, the render, the join, verify."""

    def bundle(self, d):
        (d / "job.env").write_text('PAGE=examples/hello/index.html\nSIZE=320x180\nPARAMS="seconds=4"\nENCODER=x264\n'
                                   "JOB_FRAMES=60\nCUTS=120\nNAME=hello\nMAX_WORKERS=2\n")
        r = subprocess.run(["bash", RUNPOD / "launch.sh", "--site", ROOT, "--job", d / "job.env", "--runs", d / "runs",
                            "--bundle-out", d / "bundle.tar"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dry run", r.stdout)
        with tarfile.open(d / "bundle.tar") as t:
            names = t.getnames()
            self.assertIn("farm/job.env", names)
            self.assertIn("farm/cloud/runpod/job.sh", names)
            self.assertIn("farm/nvenc-gpu-filter/nvenc-gpu-filter.c", names)
            self.assertFalse([n for n in names if "/.git/" in n or "__pycache__" in n])
            t.extractall(d / "pod")

    def job(self, d, **extra):
        env = {**os.environ, "WORKSPACE": str(d / "ws"), "SKIP_SETUP": "1", "PYTHON": PY,
               "WEBGL2VIDEO_PROGRESS_SECONDS": "1", "LADDER_MIN_WINDOW": "1", "MAX_SECONDS": "900", **extra}
        r = subprocess.run(["bash", d / "pod" / "farm" / "cloud" / "runpod" / "job.sh"], capture_output=True, text=True,
                           env=env, timeout=900)
        return r.returncode, r.stdout + r.stderr

    def finished(self, d, log):
        for want in ("FRAMES 240 (4.00 s at 60 fps)", "ALL DONE (0)"):
            self.assertIn(want, log)
        self.assertTrue((d / "ws" / "deliver" / "hello.mp4").exists())
        checks = [json.loads(p.read_text()) for p in (d / "ws" / "deliver" / "checks").glob("check-*.json")]
        self.assertEqual(len(checks), 4)
        self.assertEqual([f for c in checks for f in c["flags"]], [])  # the cut at 120 is declared in CUTS
        self.assertIn(120, [k["frame"] for c in checks for k in c["known_cuts"]])

    def test_job(self):
        d = tmpdir(self)
        self.bundle(d)
        code, log = self.job(d, BUDGET="1")
        self.assertEqual(code, 0, log[-3000:])
        for want in ("GPU conversion: exact", "STAGE s1-w1", "STAGE s2-w2", "AUTO go 2", "DECISION: go 2"):
            self.assertIn(want, log)
        self.finished(d, log)

    def test_halt_then_resume(self):
        """No decision: the job stops with its work kept. Run again (a restarted pod), it skips what is done, decides
        from the scaling test it measured before, and finishes."""
        d = tmpdir(self)
        self.bundle(d)
        code, log = self.job(d, DECIDE_MINUTES="0")
        self.assertEqual(code, 2, log[-3000:])
        self.assertIn("STOPPING (work kept): no decision in 0 min", log)
        self.assertEqual(len(list((d / "ws" / "done").iterdir())), 3)  # the scaling test's jobs, kept
        code, log = self.job(d, BUDGET="1")
        self.assertEqual(code, 0, log[-3000:])
        for want in ("STAGE s1: its jobs were done in an earlier run: skipped", "STAGE s2: its jobs were done in an earlier run",
                     "AUTO go ", "PHASE render (4 jobs, 3 done; ", "PROGRESS 240 of 240 frames"):
            self.assertIn(want, log)  # (on jobs this small both tested counts can measure the same rate: either goes)
        self.assertEqual(log.count("JOB "), 1, log)  # only the job left was rendered
        self.finished(d, log)


if __name__ == "__main__":
    unittest.main()
