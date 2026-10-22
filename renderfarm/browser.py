"""The browser side of a render: a frame sink on localhost, and one warm Chromium per worker with the page loaded.

The page (page/render-page.js) draws frame i on the GPU, converts it into the bytes the encoder takes and POSTs them
to the sink. Raw pixels over a local socket cost a few milliseconds per 4K frame; a base64 PNG through the browser
automation protocol cost close to a second. The driver asks for frame i + 1 before it writes frame i to ffmpeg, and the
page draws frame i + 1 while it reads frame i back, so the GPU, the page and the encoder overlap.
"""

import base64
import concurrent.futures
import hashlib
import http.server
import mimetypes
import pathlib
import queue
import re
import sys
import threading
import urllib.parse

from .util import first_line, log

# Browser launch profiles. SwiftShader is Chromium's CPU implementation of WebGL2: slow, but it runs anywhere.
CPU_PROFILE = {"name": "SwiftShader (CPU)", "channel": None, "headless": True, "args": [
    "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist", "--disable-dev-shm-usage", "--no-sandbox"]}
# This machine's GPU: Chromium's new headless mode first, then a small visible window (a browser with a window always
# gets the GPU on macOS).
GPU_PROFILES = [
    {"name": "GPU, headless", "channel": "chromium", "headless": True,
     "args": ["--enable-gpu", "--ignore-gpu-blocklist"]},
    {"name": "GPU, in a small window", "channel": "chromium", "headless": False,
     "args": ["--ignore-gpu-blocklist", "--window-size=360,720"]},
]
# A Linux server with an NVIDIA GPU (a cloud pod: no display, no X server): ANGLE on Vulkan, then on EGL. The container
# must expose the driver's graphics libraries (NVIDIA_DRIVER_CAPABILITIES=all); without them both fall back to
# software, which --require-gpu refuses. ANGLE's OpenGL backend reads 4K frames back faster than its Vulkan one but
# draws some edges and points differently: never mix backends within one film (RENDERFARM_GPU_PROFILE pins one).
if sys.platform.startswith("linux"):
    GPU_PROFILES[:0] = [
        {"name": "GPU (Linux, ANGLE on Vulkan), headless", "channel": "chromium", "headless": True,
         "args": ["--enable-gpu", "--ignore-gpu-blocklist", "--use-angle=vulkan", "--enable-features=Vulkan",
                  "--disable-vulkan-surface", "--no-sandbox", "--disable-dev-shm-usage"]},
        {"name": "GPU (Linux, ANGLE on EGL), headless", "channel": "chromium", "headless": True,
         "args": ["--enable-gpu", "--ignore-gpu-blocklist", "--use-gl=angle", "--use-angle=gl-egl", "--no-sandbox",
                  "--disable-dev-shm-usage"]},
    ]
VIEWPORT = {"width": 360, "height": 640}  # frames come from the page's own render targets: the window can stay small
FRAME_TIMEOUT = 300  # s: a frame that takes longer than this means the browser hung
BOOT_TIMEOUT = 600   # s: the page's own setup (compiling, generating textures) can take minutes in SwiftShader

FORMATS = ("rgba", "nv12", "x2bgr10", "p010")


def frame_bytes(fmt: str, w: int, h: int) -> int:
    """The bytes the page posts per frame (page/render-page.js FORMATS)."""
    if fmt == "nv12":
        return w * h * 3 // 2
    if fmt == "p010":
        return w * h * 3 + 16  # p010le, then 16 bytes of light levels
    return w * h * 4  # rgba, x2bgr10le


def is_hdr(fmt: str) -> bool:
    return fmt in ("x2bgr10", "p010")


def is_software(renderer: str) -> bool:
    r = str(renderer).lower()
    return any(k in r for k in ("swiftshader", "llvmpipe", "lavapipe", "software", "basic render"))


class EngineTimeout(RuntimeError):
    pass


class PageStop(RuntimeError):
    """The page cannot render: its own error (window.__RENDER_ERROR), or it is not what the driver asked for."""


TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json",
         ".wasm": "application/wasm", ".svg": "image/svg+xml", ".glsl": "text/plain; charset=utf-8",
         ".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg"}


class FrameSink:
    """A small HTTP server on localhost, for one browser: it serves the page's folder (so the page and the sink share
    an origin) and receives each rendered frame, POSTed by the page to /frame/<index>. It also remembers a hash of every
    file it served: the render's key, so a resumed render never mixes segments of two versions of a page."""

    def __init__(self, root, frame_bytes: int):
        self.root = pathlib.Path(root).resolve()
        self.frame_bytes = frame_bytes
        self.served = {}  # path -> sha1 of the bytes served
        self._waiting = {}  # frame index -> Future resolved with the bytes when the page posts them
        self._lock = threading.Lock()
        sink = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep the connection open across frames

            def log_message(self, *a):
                pass

            def _empty(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                rel = urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/")
                f = (sink.root / rel).resolve()
                if not (f == sink.root or sink.root in f.parents) or not f.is_file():
                    return self._empty(204 if rel == "favicon.ico" else 404)
                data = f.read_bytes()
                sink.served[rel] = hashlib.sha1(data).hexdigest()
                self.send_response(200)
                self.send_header("Content-Type", TYPES.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0]
                                 or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                m = re.fullmatch(r"/frame/(\d+)", self.path)
                n = int(self.headers.get("Content-Length") or 0)
                if not m or n != sink.frame_bytes:
                    self.rfile.read(n)
                    return self._empty(400)
                data = self.rfile.read(n)
                with sink._lock:
                    fut = sink._waiting.pop(int(m.group(1)), None)
                if fut is not None and not fut.done():
                    fut.set_result(data)
                self._empty(200)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, name="frame-sink", daemon=True).start()

    def key(self) -> str:
        """A hash of every file the page loaded."""
        h = hashlib.sha1()
        for rel in sorted(self.served):
            h.update(f"{rel}\0{self.served[rel]}\n".encode())
        return h.hexdigest()[:12]

    def expect(self, i: int) -> concurrent.futures.Future:
        """A future for frame i, resolved with its bytes when the page posts them."""
        fut = concurrent.futures.Future()
        with self._lock:
            self._waiting[i] = fut
        return fut

    def fail(self, exc: BaseException, keep=None):
        """Fail every frame still expected (the browser is being restarted), except `keep`, which the retry resolves."""
        with self._lock:
            kept = [(i, f) for i, f in self._waiting.items() if f is keep]
            failed = [f for f in self._waiting.values() if f is not keep]
            self._waiting = dict(kept)
        for fut in failed:
            if not fut.done():
                fut.set_exception(exc)

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def page_query(cfg: dict) -> str:
    q = [("render", "1"), ("w", cfg["width"]), ("h", cfg["height"]), ("format", cfg["format"])]
    if cfg.get("fps"):
        q.append(("fps", cfg["fps"]))
    for kv in cfg.get("params") or []:
        k, _, v = kv.partition("=")
        q.append((k, v))
    return urllib.parse.urlencode(q)


def open_page(pw, url: str, cfg: dict, profile: dict, require_hw: bool = False):
    """Launch a browser with the page loaded and ready. Returns (browser, page, info)."""
    # --expose-gc: the driver collects garbage every few frames (Engine._frame). Each frame travels to the sink in a
    # Blob (33 MB at 4K), freed only when the page's garbage collector runs; with readback buffers reused, the collector
    # rarely ran on its own and the Blobs piled up in Chrome's blob store until uploads failed (frame 16 at 4K).
    kw = {"args": profile["args"] + ["--js-flags=--expose-gc"], "headless": profile.get("headless", True)}
    if profile.get("channel"):
        kw["channel"] = profile["channel"]
    browser = pw.chromium.launch(**kw)
    try:
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        page.on("pageerror", lambda e: log("page error:", first_line(e)))
        seen = set()

        def on_console(m):  # the page's warnings and errors, once each
            if m.type in ("warning", "error") and m.text not in seen and len(seen) < 40:
                seen.add(m.text)
                log(f"page {m.type}:", first_line(m.text))
        page.on("console", on_console)
        page.goto(url, wait_until="load", timeout=120_000)
        page.wait_for_function("window.__RENDER_READY === true || !!window.__RENDER_ERROR", polling=200,
                               timeout=BOOT_TIMEOUT * 1000)
        err = page.evaluate("() => window.__RENDER_ERROR || null")
        if err:
            raise PageStop("the page stopped: " + " ".join(str(err).split()))
        info = page.evaluate("""() => { const r = window.__RENDER; return { version: r.version, fps: r.fps,
            frames: r.frameCount, width: r.width, height: r.height, format: r.format, frameBytes: r.frameBytes,
            gl: r.glRenderer, audio: typeof r.audioRender === 'function', hdr: r.hdr || null, info: r.info ?? null }; }""")
        want = {"width": cfg["width"], "height": cfg["height"], "format": cfg["format"],
                "frameBytes": frame_bytes(cfg["format"], cfg["width"], cfg["height"])}
        bad = [f"{k} {info.get(k)!r}, not {v!r}" for k, v in want.items() if info.get(k) != v]
        if bad:
            raise PageStop("the page renders something else than asked: " + "; ".join(bad))
        if cfg.get("fps") and abs(float(info["fps"]) - float(cfg["fps"])) > 1e-6:
            raise PageStop(f"the page runs at {info['fps']} fps, not the {cfg['fps']} asked for")
        if require_hw and is_software(info["gl"]):
            raise RuntimeError(f"no hardware WebGL here (renderer: {info['gl']})")
    except BaseException:
        browser.close()
        raise
    return browser, page, info


class _Thread:
    """Runs calls one at a time on a single dedicated daemon thread."""

    def __init__(self):
        self._q = queue.Queue()
        threading.Thread(target=self._loop, name="playwright", daemon=True).start()

    def _loop(self):
        while True:
            fn, fut = self._q.get()
            if fn is None:
                return
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as e:
                    fut.set_exception(e)

    def submit(self, fn) -> concurrent.futures.Future:
        fut = concurrent.futures.Future()
        self._q.put((fn, fut))
        return fut

    def stop(self):
        self._q.put((None, None))


class Engine:
    """One browser with the page loaded, kept warm across frames: the page sets itself up once, then each frame only
    renders.

    Playwright's sync API is bound to the thread that started it, so every browser call runs on one dedicated thread,
    whichever thread asks. That also lets the caller encode one frame while the next renders."""

    def __init__(self, page, root, cfg: dict, profile: dict = CPU_PROFILE, require_hw: bool = False):
        self.page_path = pathlib.Path(page).resolve()
        self.root = pathlib.Path(root).resolve()
        self.cfg, self.profile, self.require_hw = cfg, profile, require_hw
        self.info = None
        self.stages = []  # in-page stage timings (ms) of the last frames, newest last
        self._thread = _Thread()
        self._pw = self._browser = self._page = None
        self._sink = FrameSink(self.root, frame_bytes(cfg["format"], cfg["width"], cfg["height"]))
        rel = self.page_path.relative_to(self.root).as_posix()
        self.url = f"{self._sink.url}/{urllib.parse.quote(rel)}?{page_query(cfg)}"

    def _run(self, fn, timeout: float):
        return self.wait(self._thread.submit(fn), timeout)

    @staticmethod
    def wait(fut, timeout: float = FRAME_TIMEOUT):
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise EngineTimeout(f"the browser did not respond for {timeout:.0f} s") from None

    def open(self) -> dict:
        self._run(self._open, BOOT_TIMEOUT + 180)
        return self.info

    def key(self) -> str:
        """A hash of every file the page loaded (FrameSink.key)."""
        return self._sink.key()

    def frame_async(self, i: int, next_i: int = None) -> concurrent.futures.Future:
        """Frame i's bytes (in the page's format), rendered meanwhile: the future resolves when the page's upload
        reaches the sink. `next_i`, the frame that will be asked for after this one, starts rendering on the GPU while
        frame i is read back and sent. Keep two requests queued (render_frames does) so the GPU never waits on Python."""
        fut = self._sink.expect(i)
        self._thread.submit(lambda: self._frame(i, next_i, fut))
        return fut

    def frame_raw(self, i: int) -> bytes:
        return self.wait(self.frame_async(i))

    def audio_wav(self, sample_rate: int = 48000) -> bytes:
        """The page's own soundtrack (a WAV file's bytes), in pieces of 8 MB."""
        meta = self._run(lambda: self._page.evaluate("sr => __RENDER.audioRender(sr)", sample_rate), 1800)
        parts = [self._run(lambda k=k: self._page.evaluate("k => __RENDER.audioChunk(k)", k), 300)
                 for k in range(meta["chunks"])]
        data = b"".join(base64.b64decode(p) for p in parts)
        if len(data) != meta["bytes"]:
            raise RuntimeError(f"the soundtrack arrived as {len(data)} bytes of {meta['bytes']}")
        return data

    def evaluate(self, expr: str, arg=None, timeout: float = FRAME_TIMEOUT):
        """page.evaluate on the engine thread."""
        return self._run(lambda: self._page.evaluate(expr, arg), timeout)

    def close(self):
        try:
            self._run(self._shutdown, 60)
        except Exception:
            pass
        self._thread.stop()
        self._sink.close()

    # The methods below run on the engine thread only.
    def _open(self):
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._close_browser()
        self._browser, self._page, self.info = open_page(self._pw, self.url, self.cfg, self.profile, self.require_hw)

    def _frame(self, i: int, next_i, fut: concurrent.futures.Future):
        """Ask the page for frame i (its bytes resolve `fut` through the sink). A page failure here also covers the
        previous frame's upload, which the page checks at the end of this call."""
        for attempt in (1, 2):
            try:
                timing = self._page.evaluate("([i, url, next]) => { if (i % 4 === 0 && typeof gc === 'function') gc();"
                                             " return __RENDER.frameTo(i, url, next); }",
                                             [i, f"{self._sink.url}/frame/{i}", next_i])
                self.stages.append(timing)
                del self.stages[:-64]
                return
            except Exception as e:
                if attempt == 2:
                    self._sink.fail(RuntimeError(f"frame {i} failed twice: {first_line(e)}"))
                    return
                log(f"frame {i} failed ({first_line(e)}); restarting the browser and trying again")
                self._sink.fail(RuntimeError(f"frame {i} failed: {first_line(e)}"), keep=fut)
                try:
                    self._open()
                except Exception as e2:  # fail this frame too, with the reason, rather than leave it to time out
                    self._sink.fail(e2 if isinstance(e2, PageStop) else
                                    RuntimeError(f"frame {i}: the browser did not restart: {first_line(e2)}"))
                    return

    def _close_browser(self):
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = self._page = None

    def _shutdown(self):
        self._close_browser()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None


def preferred_profiles() -> list:
    """GPU_PROFILES with a chosen one first: RENDERFARM_GPU_PROFILE (its index, or a word of its name)."""
    import os
    want = (os.environ.get("RENDERFARM_GPU_PROFILE") or "").lower()
    if not want:
        return GPU_PROFILES
    first = [p for i, p in enumerate(GPU_PROFILES) if want == str(i) or want in p["name"].lower()]
    return first + [p for p in GPU_PROFILES if p not in first]


def pick_profile(page, root, cfg: dict):
    """The first launch profile that gets hardware WebGL here, tried on a small frame: (profile, info), or (None,
    None)."""
    small = dict(cfg, width=256, height=144)
    for prof in preferred_profiles():
        eng = Engine(page, root, small, prof)
        try:
            info = eng.open()
            if not is_software(info["gl"]):
                return prof, info
            log(f"  {prof['name']}: software rendering only ({info['gl']})")
        except PageStop:
            raise
        except Exception as e:
            log(f"  {prof['name']}: not available ({first_line(e)})")
        finally:
            eng.close()
    return None, None


def default_root(page) -> pathlib.Path:
    """The folder served to the browser: the current folder when it holds the page (so a page can import files beside
    it or above it, as the examples do), else the page's own folder."""
    page, cwd = pathlib.Path(page).resolve(), pathlib.Path.cwd().resolve()
    return cwd if cwd in page.parents else page.parent
