# webgl2video

Render any WebGL or HTML animation (Three.js, shaders, p5.js, AI-generated pages) to frame-exact 4K60 video on a GPU,
locally or on cheap cloud GPUs.

![webgl2video: frame-exact 4K60 video from any WebGL or HTML animation](docs/cover.jpg)

*A frame from a 10-minute 4K60 science film rendered with it. Below: `examples/hello`, frames 75 and 330.*

![Two frames of the example page, either side of its hard cut](docs/hello.jpg)

## What it solves

**1. Exact video from an HTML animation.** Recording a screen, or `MediaRecorder` on a canvas, drops frames and runs in
real time. Rendering in the page (CCapture.js and its successors) keeps frames exact but runs in one browser tab and keeps
the frames in its memory until it saves them. webgl2video drives the page from outside instead: headless Chromium renders frame `i` when
asked, the page converts it to the encoder's own pixel format on the GPU, and the bytes stream to ffmpeg, which writes
resumable segments. Any length, any size up to 4K60 and beyond, SDR or HDR10, a soundtrack the page renders itself, a
film rendered in pieces on many machines and joined without re-encoding.

A page that draws on a canvas with any API hands that canvas over (`exposeRenderer({ canvas, draw })`): Canvas 2D and
Three.js are tested (`examples/canvas2d`, `examples/threejs`); other libraries that draw on a canvas (p5.js, PixiJS, a
page an AI wrote) take the same route, as long as frame `i` depends on `i` alone ([docs/page-contract.md](docs/page-contract.md)).

**2. Real GPU WebGL in headless Chrome on a Linux server.** In Docker or on a cloud GPU, headless Chromium quietly falls
back to SwiftShader (WebGL on the CPU) while `nvidia-smi` works fine beside it
([Docker forums](https://forums.docker.com/t/hardware-acceleration-for-headless-chrome-with-nvidia-gpu/132557),
[Fly.io](https://community.fly.io/t/use-gpu-acceleration-on-puppeteer/25489)). `python -m webgl2video gpucheck` prints
the WebGL renderer Chromium got and **exits 1 on a software renderer**; [docs/headless-gpu.md](docs/headless-gpu.md) is
the recipe (driver capabilities, Vulkan, Chromium's flags) that renders on NVIDIA GPUs in containers.

**3. GPU cloud for WebGL-heavy renders.** Serverless render farms have no GPU: [Remotion's
docs](https://www.remotion.dev/docs/gpu) say so of AWS Lambda, so WebGL there runs on the CPU. webgl2video
rents one GPU pod on RunPod, renders the whole film with parallel workers, checks it, optionally uploads it to YouTube,
and deletes the pod. Measured on 10-minute 4K60 films (about 36,000 frames each):

| | time | cost |
|---|---|---|
| a MacBook (Apple M2), one browser | more than 12 hours | |
| RTX 4000 Ada (RunPod Secure, $0.28/h), first real run | 109 min of pod, with lessons since fixed | $0.53 |
| the same, a clean run | about 51 min, projected | about $0.25 |

(It works with any deterministic page; it does not integrate with Remotion.)

**4. NVENC in containers on multi-GPU hosts.** With NVIDIA's 570-580 drivers, NVENC fails in a container given one GPU
of a multi-GPU host unless it is the last one
([nvidia-container-toolkit#1249](https://github.com/NVIDIA/nvidia-container-toolkit/issues/1249), closed without a fix
in the toolkit). [nvenc-gpu-filter](nvenc-gpu-filter/README.md) is a standalone `LD_PRELOAD` fix: one C file, fail-safe,
with a test that runs without a GPU.

## Quickstart

Needs Python 3.9+, ffmpeg, and a GPU the browser can use (without one it renders with SwiftShader, slowly).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m playwright install chromium
python -m webgl2video gpucheck
```

Render the example (10 seconds, two shots, a soundtrack the page renders itself):

```bash
python -m webgl2video render examples/hello/index.html --size 1920x1080 --out hello.mp4
```

A Canvas 2D page, and a Three.js scene (it loads Three.js from a CDN):

```bash
python -m webgl2video render examples/canvas2d/index.html --size 1920x1080 --out canvas2d.mp4
```

```bash
python -m webgl2video render examples/threejs/index.html --size 3840x2160 --out threejs.mp4
```

Check that the GPU colour conversion is exact on your machine, then render in HDR10 (the master needs MKVToolNix):

```bash
python -m webgl2video exact examples/hello/index.html --hdr
```

```bash
python -m webgl2video render examples/hello/index.html --size 3840x2160 --hdr --out hello-hdr.mkv
```

Interrupt a render and run the same command again: it resumes. `--frames A:B --segments-only` renders one piece;
`qa` checks a rendered range for unexpected cuts and single-frame flashes; `verify` checks a finished video (frames,
loudness, sync with the soundtrack). `python -m webgl2video <command> --help` for each. To preview an example in a
browser, serve the repository (`python3 -m http.server`) and open `/examples/hello/`.

## Your page

Two ways, both in [docs/page-contract.md](docs/page-contract.md). Draw on a canvas with anything:

```js
import { renderParams, exposeRenderer } from './render-page.js';
const P = renderParams();                                  // ?render=1&w=..&h=..&format=.. from the driver
if (P.render) { canvas.width = P.width; canvas.height = P.height; }
if (P.render) exposeRenderer({ canvas, fps: 60, frameCount: 3600, draw: (i) => drawFrame(i / 60) });
```

or, with raw WebGL2, render into the helper's target (also the way to HDR10):

```js
if (P.render) exposeRenderer({ gl, fps: 60, frameCount: 3600, draw: (i, target) => { /* frame i into target */ } });
```

Frame `i` must depend on `i` alone: no clock, no unseeded randomness, no state carried between frames.

**How a frame travels.** The page draws frame `i`; `render-page.js` quantises it (8 or 10 bits), converts it on the GPU to
NV12 (BT.709) or P010 (BT.2020) with the chroma sited left, meters HDR light (MaxCLL, MaxFALL), reads it back while
drawing frame `i + 1`, and posts it to the driver on localhost; ffmpeg encodes it (NVENC, VideoToolbox, x264, x265) into
segments with a keyframe every second. The conversion matches a float64 reference to within one code
(`python -m webgl2video exact`), and it is why the pipeline is fast: on a cloud GPU the render is bound by the CPU, not
the GPU, and ffmpeg's own conversion cost a worker nearly as much CPU as the page did. On `examples/hello` at 4K, one worker on
an M2 MacBook Air renders 36 frames/s with the GPU conversion and 21 without.

## Rendering on RunPod

[docs/runpod.md](docs/runpod.md): an API key, `runpodctl`, an SSH key, a job file, then

```bash
cloud/runpod/launch.sh --site my-site --job job.env              # a dry run: builds the bundle, spends nothing
```

```bash
cloud/runpod/launch.sh --site my-site --job job.env --go --budget 0.60
```

The launcher asks RunPod's GraphQL API for a minimum of CPU cores (which `runpodctl` cannot), reads the pod's real CPU
quota before copying anything (the cores a GPU comes with are a lottery: two RTX 4090 pods listed alike had 6.8 and 13),
and sends a checksummed bundle. The pod then runs by itself: an exactness check on its GPU, a scaling test at 1, 4 and N
workers on real pieces of the film with minutes and dollars projected for each, a decision (yours, or automatic within
`--budget`), workers on a shared queue of 500-frame jobs, a QA check of each job, the join, the checks, an optional
Private upload to YouTube, the results back to your machine, and its own deletion. A watchdog caps the cost; a failure
after rendering began stops the pod instead, keeping the work for a resume. Watch it live with
`python3 cloud/runpod/logserver.py <run folder>`. What was measured along the way: [docs/findings.md](docs/findings.md).

## Tests

```bash
python -m unittest discover -s tests -v
```

About two minutes on a laptop, no cloud and no network: the exactness test (NV12, and P010 with its light
meter, against a float64 reference), canvas mode giving the same bytes as the WebGL path, an end-to-end render with
resume, pieces rendered apart that come out byte-identical, the QA finding the example's hard cut (also at a seam
between jobs), HDR metering on the GPU and the CPU alike, the HDR10 join, `gpucheck` failing on SwiftShader, the job
queue and the scaling test's arithmetic, the deploy request, the NVENC shim against a mock driver, the YouTube uploader
against a local stand-in for Google, and the whole cloud job run on this machine with its setup skipped (straight
through, and stopped at its decision then resumed).

Not covered: the paid parts (a real pod, NVENC, RunPod's API, YouTube itself). Those come from the scripts that rendered
and uploaded the films this was built for, with an independent review's fixes ([docs/left-out.md](docs/left-out.md)).

## Layout

```
page/render-page.js     the page side: the contract, canvas mode, GPU conversion, light meter, readback
webgl2video/            the driver: render, gpucheck, probe, exact, qa, verify, upload
cloud/runpod/           launch.sh, deploy.py, job.sh (the pod), pod.sh, queue.py, ladder.py, logserver.py, ...
nvenc-gpu-filter/       the NVENC fix for containers on multi-GPU hosts, with its own README
examples/               hello (WebGL2, HDR, soundtrack), canvas2d (Canvas 2D), threejs (Three.js)
docs/                   the page contract, headless GPU, RunPod, YouTube, findings, what was left out
tests/                  unittest
```

## License

[MIT](LICENSE).
