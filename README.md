# webgl-render-farm

*(a working name)*

Render any deterministic WebGL page into a frame-exact video, up to 4K60 in SDR or HDR10, on your own GPU or on a
rented cloud GPU: parallel headless Chromium workers, the colour conversion done on the GPU, NVENC encoding, a shared
job queue, automatic checks, cost control, a pod that cleans up after itself (deleted when done, stopped with its work
kept when something fails mid-render), and an optional upload straight to YouTube.

It was built to render a science channel's films (10-minute 4K60 WebGL films of about 36,000 frames), which took more
than 12 hours each on a MacBook. On a $0.28/hour RunPod GPU, the first real run of a film cost **$0.53**; a clean run
projects to **about 51 minutes and $0.25**.

![Two frames of the example page, either side of its hard cut](docs/hello.jpg)

*The example page (`examples/hello`), frames 75 and 330, as the driver renders them.*

## Why

A WebGL animation in which every frame is a pure function of its index can be rendered like a film, not recorded like
a screen: at any size and frame rate, slower or faster than real time, in pieces on many machines, resumed after any
interruption, and joined without re-encoding. Screen capture and `MediaRecorder` can do none of that, and drop frames.

Doing it fast and cheap turned out to be a CPU problem, not a GPU one (all numbers in [docs/findings.md](docs/findings.md)):

- **The GPU sat mostly idle.** On the first cloud run an RTX 4090 was 31% busy; each worker needed about 2.5 cores
  (the browser's page thread, ffmpeg's RGB to Y'CbCr conversion, an HDR light meter in numpy). So the conversion and
  the light meter moved into the page's own GPU passes, exact to a float64 reference, which cut the CPU per 4K HDR frame
  in half. On the example page here, one worker renders 4K at 36 frames/s with the GPU conversion and 21 without.
- **The cores a cloud GPU comes with are a lottery.** Two Secure RTX 4090 pods in one datacenter, both listed with 8+
  vCPU, had CPU quotas of 6.8 and 13 cores. The launcher asks the API for a minimum of cores (which `runpodctl` cannot)
  and reads the real cgroup quota before copying anything, rejecting a weak host for $0.02.
- **Pick the GPU by CPU, NVENC sessions and price.** With the GPU mostly idle, a weaker GPU with the same CPU renders
  about as fast: the fastest run, on an RTX 4000 Ada (more cores, the GPU conversion), cost a third of a 4090 an hour.
  GeForce cards stop at 8 encoder sessions; A100, H100 and B200 have no NVENC at all.
- **Each worker slows as workers are added** (the page thread, the GPU's readback queue): on an RTX 4000 Ada, 1 worker
  rendered 3.0 frames/s, 4 workers 6.6, 11 workers 15.4. Every render measures its own ladder on real pieces of the film
  before you commit to it, and prints the minutes and dollars for each worker count.

| 10-minute 4K60 film | time | cost |
|---|---|---|
| MacBook (Apple M2), one browser | more than 12 hours | |
| RTX 4090 Secure, first run (CPU-side conversion, a 6.8-core host) | 1.9 h of pod | $1.67 |
| RTX 3090 Secure (31 cores, capped at 8 workers by GeForce NVENC) | about 82 min, projected | about $0.68 |
| RTX 4000 Ada Secure (13.6 cores, 11 workers), first real run | 109 min of pod (with a decision wait and an idle tail since fixed) | $0.53 |
| RTX 4000 Ada Secure, clean run | about 51 min, projected | about $0.25 |

## How it works

```
 the page, on the GPU (render-page.js)    the driver (python -m renderfarm)    ffmpeg
 draw(i)                                  asks for frame i+1                   encodes (NVENC, x264, x265)
 quantise to 8 or 10 bits                 before it writes frame i             into segments, seg_A-B.mp4,
 convert to NV12 or P010                  a frame sink on localhost            joined by stream copy
 meter HDR light (MaxCLL, MaxFALL)        receives POST /frame/i  ──────────►  with the soundtrack
 read back i while drawing i+1  ────────►
```

- **The page** follows a small contract ([docs/page-contract.md](docs/page-contract.md)): it draws frame `i` when asked.
  `page/render-page.js` does the rest on the GPU: two slots so frame `i + 1` renders while frame `i` is read back,
  8-bit or 10-bit quantisation, BT.709 NV12 or BT.2020 P010 with left-sited chroma, the MaxCLL/MaxFALL meter, and the
  upload to the driver as a `Blob` (twenty times faster than an `ArrayBuffer` body).
- **The driver** (`renderfarm/`) runs one warm headless Chromium per worker through Playwright, refuses software
  WebGL on paid machines, restarts the browser once if a frame fails, writes resumable segments (a keyframe every
  second, no B-frames) and joins them by stream copy with the soundtrack, which the page can render itself
  (`OfflineAudioContext`). HDR10 masters are MKVs with the ST 2086 and MaxCLL/MaxFALL metadata written in place.
- **The cloud** (`cloud/runpod/`): `launch.sh` deploys a pod through RunPod's GraphQL API with a minimum of cores and a
  public IP, checks its real CPU quota, sends a checksummed bundle, and starts `job.sh`, which runs by itself:
  setup, an exactness check of the GPU conversion on that GPU, a 1/4/N-worker scaling test on real jobs, a decision
  (yours, or automatic under `--budget`), workers claiming 500-frame jobs from a shared queue, a QA check of each job
  (unexpected cuts, single-frame flashes, the seams between jobs), the join, checks of the master (frames, loudness,
  sync), an optional Private upload to YouTube, the results back to your machine, and its own deletion. A watchdog
  outlives the job; a failure after rendering began stops the pod instead, keeping the work for a resume.

## Quickstart (your own machine, free)

Needs Python 3.9+, ffmpeg, and a GPU the browser can use (it falls back to SwiftShader, slowly, without one).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m playwright install chromium
```

Render the example (10 seconds, two shots, a soundtrack the page renders itself):

```bash
python -m renderfarm render examples/hello/index.html --size 1920x1080 --out hello.mp4
```

Check that the GPU conversion is exact on your machine, then render it in HDR10 (the master needs MKVToolNix):

```bash
python -m renderfarm exact examples/hello/index.html --hdr
```

```bash
python -m renderfarm render examples/hello/index.html --size 3840x2160 --hdr --out hello-hdr.mkv
```

Interrupt any render and run the same command again: it resumes. `--frames A:B --segments-only` renders one piece (a
cloud worker's job); `python -m renderfarm qa` checks a rendered range; `python -m renderfarm verify` checks a finished
video. `python -m renderfarm <command> --help` for all of them. To preview the example in a browser, serve the
repository (`python3 -m http.server`) and open `/examples/hello/`: it plays in real time when not rendering.

## Your own page

Follow [docs/page-contract.md](docs/page-contract.md). With the helper it is a few lines:

```js
import { renderParams, exposeRenderer } from './render-page.js';
const P = renderParams();                      // ?render=1&w=..&h=..&format=.. from the driver
// ... create the WebGL2 context, compile, load everything ...
if (P.render) exposeRenderer({ gl, fps: 60, frameCount: 3600, draw: (i, target) => { /* frame i, from i alone */ } });
```

`draw(i, target)` must depend on `i` only: no clock, no unseeded randomness, no state carried between frames.

## Rendering on RunPod

[docs/runpod.md](docs/runpod.md): the account (an API key, `runpodctl`, an SSH key, optional YouTube secrets), the job
file, then:

```bash
cloud/runpod/launch.sh --site my-site --job job.env              # a dry run: builds the bundle, spends nothing
```

```bash
cloud/runpod/launch.sh --site my-site --job job.env --go --budget 0.60
```

Watch it live with `python3 cloud/runpod/logserver.py <run folder>`. Results, QA sheets and logs come back to
`~/.renderfarm/runs/`.

## Tests

```bash
python -m unittest discover -s tests -v
```

About two minutes on a laptop, no cloud: the exactness test (NV12, and P010 with its light meter, against a
float64 reference; and how far ffmpeg's own conversion is from it), an end-to-end render with resume, pieces rendered
apart that come out byte-identical, the QA finding the example's hard cut (also across a seam between jobs), the
verification, HDR segments metered on the GPU and on the CPU alike, the HDR10 join, the job queue and the scaling
test's arithmetic, the deploy request, the NVENC shim against a mock driver, the YouTube uploader against a local
stand-in for Google (a server error and an expired token mid-upload), and the whole pod job run on this machine with its
setup skipped: once straight through, once stopped at its decision and resumed.

What the tests cannot cover: the paid parts (a real pod, NVENC, RunPod's API, YouTube itself). Those are the scripts
that rendered and uploaded the films, with the film-specific parts taken out and the gaps an independent review found
fixed ([docs/left-out.md](docs/left-out.md)).

## Layout

```
page/render-page.js      the page side: the contract, GPU conversion, light meter, readback, upload
renderfarm/              the driver: render, probe, exact, qa, verify, upload
cloud/runpod/            launch.sh, deploy.py, job.sh (the pod), pod.sh, queue.py, ladder.py, logserver.py, ...
cloud/nvenc-gpu-filter.c the NVENC fix for containers on multi-GPU hosts (nvidia-container-toolkit#1249)
examples/hello/          a tiny deterministic page: two shots, HDR highlights, an offline soundtrack
docs/                    the page contract, RunPod, YouTube, the measured findings, what was left out
tests/                   unittest, no cloud
```

## License

Not chosen yet: see [LICENSE](LICENSE).
