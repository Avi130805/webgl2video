# What was left out, and why

This repository is the generic part of a private render pipeline built for a science channel's films. The films, the
engine that draws them, and everything specific to them stay private. Left out:

**The films themselves.** The renderer (a ray tracer, a star field, the colour grade), the episodes (their scripts,
shots, captions, narration, music), the player they are reviewed in, their thumbnails, subtitles and chapters, and every
frame or sound of them. In their place: `examples/hello`, a tiny page written for this repository.

**What only made sense for those films.**
- The checks of the render page against the review player (the same plan, the same paused frames, the same
  soundtrack) and the star-field fallback check: they compared two builds of one specific app. The generic version is
  the contract's own checks (size, format, frame count, hardware WebGL) and the exactness test.
- The HDR formation of the films' picture (tone mapping for PQ, type laid over it at 203 nits) and the LUT that
  reproduces their SDR grade for YouTube. `render-page.js` keeps the generic part: PQ code values in, a 10-bit
  quantisation with dither, P010 and the light meter out; `--hdr-lut` attaches any LUT you make.
- The QA crops of a fixed on-screen note, the films' cut lists, their "heavy" shot ranges and the choice of scaling
  test pieces: now the job file's `CUTS`, `STARTS` and `HEAVY`, and an even spread across the film.
- The 1080p copy made beside each 4K master, the thumbnail JPEGs under YouTube's 2 MB limit, the poster frame: one
  ffmpeg line each, for the channel's own workflow.
- Pinning the render to a commit of the films' repository, and building their page before bundling.
- Two YouTube channels and two OAuth apps behind one uploader (`YT_<CHANNEL>_APP`): now one set of variables, or
  `--profile` for several.

**Paths not taken any more.** A CPU fan-out mode on Modal (dozens of containers rendering with SwiftShader) predates
the GPU pods, which made it obsolete; a benchmarking tool and one-off probe scripts were superseded by the scaling test
inside the job. A separate "extra worker" script became `job.sh add-worker`.

**Personal and account details.** API keys, OAuth tokens and env files, pod ids, IP addresses, SSH keys and paths, run
folders, channel names and video ids, the machines' home folders, and the private repositories' history. The git history
here starts fresh.

## Changed on the way

- Canvas mode: a page that draws on a canvas with any API (Canvas 2D, Three.js) hands the canvas over, and the helper
  copies and converts it; the films used their own WebGL targets. `gpucheck` checks a machine for hardware WebGL
  without a page. The NVENC shim became a component of its own (`nvenc-gpu-filter/`, with a README).
- The page side is a library (`page/render-page.js`) any WebGL2 page can use, not a mode of one app: a page draws into
  a target the helper binds, and the helper quantises, converts, meters, reads back and posts. The films composed each
  frame on a 2D canvas before the GPU conversion; this draws straight into a WebGL target (one copy less).
- ffmpeg's own conversion (the `--convert cpu` fallback) now sites chroma left, as the GPU path and H.264/HEVC do; its
  default (the centre) differed from the GPU path by up to 7 codes in chroma.
- The QA also checks the seams between jobs (a worker that drew differently shows there), from whichever side finishes
  second.
- The job can decide by itself (`--budget`), keeps its decision for a resumed run and skips the jobs already done, has
  an exit trap that deletes or stops the pod if the script itself dies, and checks the YouTube login before rendering.
  The watchdog outlives the script. A master nobody collected stops the pod instead of going with it.
- The launcher deletes the pod on any exit before the job starts (an EXIT trap, not only Ctrl+C), and finds a pod that
  a timed-out deploy request made anyway by its unique name. `pod.sh` gained `collect` and `resume`, and finds a
  restarted pod's new SSH port.
- A finished run without an upload brings the master back with the results (the films always uploaded).
- The live view follows the pod's own log files over SSH instead of runpodctl's lagging log stream.
- The uploader asks YouTube where a dropped upload stands before resuming (the films' uploader retried the piece),
  retries the start of the session, and signs in again when the hour-long access token expires mid-upload.
- `deploy.py` also takes the key from `RUNPOD_API_KEY`, and has a `--dry-run`.

## Not exercised from this repository

Everything that costs money or needs an account: the pod's setup (apt, Vulkan, NVENC, the shim), `deploy.py` against
the API, `runpodctl`, the SSH copy and the collector, and the upload to YouTube itself (the uploader's protocol is
tested against a local stand-in for Google's endpoints). These are the production scripts with the film-specific
parts taken out; the production versions rendered and uploaded the films. The HDR10 master's last step (MKVToolNix's
`mkvpropedit`) is tested only when MKVToolNix is installed; without it, the test runs everything else and checks that
only the metadata is missing. Everything else runs in `tests/` on any machine with Chromium and ffmpeg, including the
whole pod job with its setup skipped.
