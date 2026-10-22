"""Shared by the tests: paths, and whether a browser (Playwright's Chromium) and ffmpeg are here."""

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # the renderfarm package, for the tests that import it
PAGE = ROOT / "examples" / "hello" / "index.html"
PY = sys.executable


def have_browser() -> bool:
    return importlib.util.find_spec("playwright") is not None and importlib.util.find_spec("numpy") is not None


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def rf(*args, timeout=900, env=None):
    """python -m renderfarm <args>, from the repository root: (exit code, stdout + stderr)."""
    r = subprocess.run([PY, "-m", "renderfarm", *map(str, args)], cwd=ROOT, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, **(env or {})})
    return r.returncode, r.stdout + r.stderr


def tmpdir(test) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="renderfarm-test-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def frames_in(path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                          "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return int(out.strip() or 0)
