# Rendering on RunPod

One pod renders the whole film with several workers (one headless Chromium and one ffmpeg each), on one GPU, and
deletes itself when it is done. A 10-minute 4K60 film cost $0.53 on an RTX 4000 Ada in its first real run (109 minutes
of pod, with lessons since fixed); a clean run projects to about 51 minutes and $0.25 ([findings](findings.md)).
Everything below runs from your machine; nothing is spent until `--go`.

## Once: the account

1. **An API key.** RunPod console → Settings → API Keys → create one with read/write access. Configure `runpodctl` with
   it (`runpodctl config --apiKey <key>`); `deploy.py` reads the same file (`~/.runpod/config.toml`), or
   `RUNPOD_API_KEY` from the environment. Nothing in this repository prints it.
2. **runpodctl** (github.com/runpod/runpodctl), a version with `pod get`, `pod delete`, `pod logs` and `ssh info`. The
   launcher uses it to follow the pod's log, find its SSH address, and delete it.
3. **An SSH key** the pod will accept. The launcher puts the public half in the pod's `PUBLIC_KEY` (pods made through the
   API start SSH only with one). By default it uses runpodctl's own key, `~/.runpod/ssh/runpodctl-ssh-key`
   (`runpodctl ssh add-key` creates it); `--ssh-key` takes any other.
4. **Optional, for uploads: RunPod secrets** named `yt_client_id`, `yt_client_secret` and `yt_refresh_token`
   (console → Secrets). The pod receives them as `YT_CLIENT_ID`, ...; their values never pass through your machine
   ([youtube.md](youtube.md)).

## Each render

1. Put the page and everything it loads in one folder (the "site"), and check it renders locally:
   `python -m renderfarm render site/index.html --size 1280x720 --frames 0:120`.
2. Copy `cloud/runpod/job.env.example` and fill it in (the page, the size, known hard cuts, slow ranges, the title).
3. Dry run: builds the bundle and prints the plan.

   ```bash
   cloud/runpod/launch.sh --site site --job job.env [--audio soundtrack.flac]
   ```

4. Go. The launcher tries the candidate GPUs in order, checks the pod's real CPU quota (rejecting it below 75% of the
   listing, before copying anything), copies the bundle in checksummed 8 MB pieces, starts the job and follows it.

   ```bash
   cloud/runpod/launch.sh --site site --job job.env --go
   ```

5. Watch it: `python3 cloud/runpod/logserver.py <run folder>`, then http://localhost:8787 (the job's log, the workers'
   progress, the overall speed). Or `cloud/runpod/pod.sh <run folder> log`.
6. Decide. After its scaling test the job prints a projection for each worker count it tried, and waits. For example,
   with the rates measured on an RTX 4000 Ada for a 36,060-frame film, 23 minutes in:

   ```
   PROJECTION 1 workers: 3.00 frames/s steady; the rest about 225 min; the whole pod about 248 min, $1.20 at $0.29/h
   PROJECTION 4 workers: 6.60 frames/s steady; the rest about 97 min; the whole pod about 120 min, $0.58 at $0.29/h
   PROJECTION 11 workers: 15.40 frames/s steady; the rest about 48 min; the whole pod about 71 min, $0.34 at $0.29/h
   ```

   ```bash
   cloud/runpod/pod.sh <run folder> decide "go 11"     # or "stop"
   ```

   With `--budget 0.60` on the launcher it decides by itself: the fastest tested count whose projection fits, or stop.
   No decision in `--decide-minutes` (20): the pod stops, keeping its work (below).
7. The results come back into the run folder (`~/.renderfarm/runs/<name>-<date>/deliver`): a QA sheet and JSON per
   job, stills, the scaling test's numbers, every log, and the master itself unless it went to YouTube (`--upload`).

## What the job does, and what it costs to stop it

`cloud/runpod/job.sh`, on the pod:

| phase | what | if it fails |
|---|---|---|
| setup | GPU, CPU quota, video memory; ffmpeg, Vulkan, Chromium (Playwright), the NVENC shim; NVENC opens, and how many sessions at once | the pod is deleted (after handing its logs to the launcher) |
| preflight | hardware WebGL for the page (`probe`), its frame count, the page's GPU conversion checked exact at the render size (`exact`), else ffmpeg converts; with `--upload`, the YouTube login | the pod is deleted (after handing its logs to the launcher) |
| scaling test | real jobs of the film at 1, 4 and N workers (N from cores, video memory, NVENC sessions), with the GPU (`nvidia-smi dmon`) and the CPU by process sampled | the pod stops (work kept) |
| decision | `go <n>`, `stop`, the budget, or a timeout | the pod stops (work kept) |
| render | the rest of the queue, each job checked as it lands (cuts, flashes, seams between jobs, a sheet of stills) | the pod stops (work kept) |
| join, verify | segments joined by stream copy with the soundtrack; every frame counted, loudness, sync against the soundtrack, stills | the pod stops (work kept) |
| upload | optional, Private | the pod stops (work kept) |
| end | the results to your machine, then the pod deletes itself | a master nobody collected: the pod stops instead |

The pod never outlives the job by accident: it deletes itself at the end; a watchdog deletes it after `--max-seconds`
(4 hours by default) even if the job hangs or dies (it is the cost cap: the work goes with it); an exit trap deletes it
if the script dies before rendering began, or stops it after; and the launcher deletes it, and confirms it is gone, on
any exit before the job starts (an error, a lost connection, Ctrl+C), including a pod that a timed-out request made
anyway (each attempt has a unique name the launcher looks up). `--keep-pod` keeps it after the job (until you delete it
or the watchdog does).

Once rendering has begun (in this run or an earlier one on the same volume), the pod stops rather than deletes on a
failure, and it also stops if the master is waiting in the results and nobody collected it (no upload, and the launcher
was gone): `pod.sh <run folder> collect` after starting it again.

**A stopped pod** no longer pays for its GPU, only for its volume (RunPod's storage price for a stopped pod, by the GB
and the month: delete it when you are done). Its `/workspace` keeps every finished segment, the queue, the measured
scaling test and the decision. To resume, start it in the console (the job does not restart by itself), then:

```bash
cloud/runpod/pod.sh <run folder> resume
```

It skips the jobs already done, keeps an earlier "go" (or decides again: an earlier "stop" is set aside), and finishes.
`pod.sh` finds the pod's new SSH port by itself. Or collect what it holds and delete it:
`pod.sh <run folder> collect`, then `pod.sh <run folder> delete`.

**More workers while it runs:** `cloud/runpod/pod.sh <run folder> add-worker 12` starts one more on the same queue, once
the job has decided to go (the join waits for it).

## Choosing a GPU

- Pick by **CPU allowance, NVENC sessions and price**, not by GPU speed: the render is CPU-bound until the page itself
  is heavy for the GPU. `--candidates "GPU type id|SECURE|min vCPU|$/h;..."` sets the list (RunPod's GPU type ids, as
  the console names them, e.g. `NVIDIA RTX 4000 Ada Generation`).
- Workstation cards (RTX 4000 Ada, A4500, A5000) open 16+ NVENC sessions; GeForce cards stop at 8 workers.
- A100, H100 and B200 have no NVENC: the job stops at setup.
- Secure cloud pods have public SSH; Community pods may not, and the launcher needs SSH.
- `CORES_PER_WORKER` (1.1) and `VRAM_PER_WORKER_GB` (1.6) in the job file size the worker count; both were measured on
  a heavy 4K page with the GPU conversion: measure yours with a short run and adjust.

## Files

| file | runs on | does |
|---|---|---|
| `cloud/runpod/launch.sh` | your machine | bundle, candidates, CPU check, checksummed copy, start, collect |
| `cloud/runpod/deploy.py` | your machine | the GraphQL deploy (minimum cores, public IP); `--dry-run` prints the request; `--lookup` finds a pod by name |
| `cloud/runpod/pod.sh` | your machine | decide, add a worker, log, collect, resume, ssh, delete (confirmed) |
| `cloud/runpod/logserver.py` | your machine | the live view (SSH; `rate.py` on the pod) |
| `cloud/runpod/job.sh` | the pod | everything above; `WORKSPACE=<dir> SKIP_SETUP=1 ENCODER=x264` runs it anywhere |
| `cloud/runpod/queue.py`, `ladder.py`, `cpu_sample.py`, `rate.py` | the pod | the queue, the scaling test's numbers, the CPU by process, the speed |
| `cloud/nvenc-gpu-filter.c` | the pod | the NVENC multi-GPU container fix, built at setup |
