# What was measured

These numbers come from rendering a science channel's films (10-minute 4K60 WebGL films of about 36,000 frames, SDR
and HDR10) with the pipeline this repository was extracted from, on RunPod in October 2026. That pipeline drove a
heavier page than the example here (a ray tracer, a star field, type composed on a 2D canvas), so the absolute rates
are those of that page; the shape of the results is what carries over. Prices are RunPod's list prices on the day.

## The headline

| where | 10-minute 4K60 film | cost |
|---|---|---|
| a MacBook (Apple M2), one browser | more than 12 hours | |
| RTX 4090, Secure cloud, first run (RGB frames, CPU-side YUV and light metering, a 6.8-core host) | 79 min of render, 1.9 h of pod (25 min of it bugs since fixed) | $1.67 at $0.89/h |
| RTX 3090, Secure, 31-core quota (NV12 from the GPU, 8 workers: GeForce's NVENC cap) | about 82 min projected from its scaling test | about $0.68 at $0.50/h |
| **RTX 4000 Ada, Secure, 13.6-core quota (NV12 from the GPU, 11 workers)** | 109 min of pod for the real run (with the scaling test, a 20-minute decision wait and a 20-minute idle tail since fixed); **about 51 min projected for a clean run** | **$0.53 real; about $0.25 projected** at $0.29/h |

The RTX 4000 Ada run rendered 36,060 frames that all passed the checks (frame count, loudness, sync 0 ms against the
soundtrack) and uploaded the master to YouTube from the pod. Setup took about 8 minutes: SSH 1, the bundle upload 4
(an 83 MB lossless soundtrack, from a home connection), installs 2, the checks under 1.

## The render is CPU-bound, not GPU-bound

- On the first RTX 4090 run the GPU was 31% busy and NVENC 17%. Each worker needed about 2.5 cores: the browser's page
  thread about 1.0, ffmpeg's RGB to 10-bit Y'CbCr conversion about 0.9, the numpy HDR light meter about 0.7. The pod
  rendered about 9 frames/s whether it ran 2 workers or 4.
- So a weaker GPU with the same CPU renders at the same speed for less money: choose a pod by its **CPU allowance and
  price**, not its GPU.
- Moving the conversion and the light meter into the page's GPU passes (`page/render-page.js`) cut the CPU per 4K HDR
  frame from 0.43 to 0.23 core-seconds (on the Mac; 0.31 to 0.15 without the type overlay), with identical light
  levels. On an A5000 the SDR NV12 path cut the CPU per frame by 17%.
- On the example page here (light, one worker, an M2 MacBook Air, VideoToolbox): 4K renders at 36 frames/s with the GPU
  conversion and 21 with ffmpeg's; 1080p at 102 and 60.

## The CPU quota lottery

The vCPU count RunPod lists for a GPU is not what a pod gets: its cgroup quota varies by host.

- A Secure RTX 4090 had a quota of 6.8 cores (`cpu.cfs_quota_us` 680000 per 100000) on a 32-thread Ryzen 9 7950X
  host; another Secure RTX 4090 in the same datacenter had 13. Both were listed with a minimum of 8 vCPU.
- An RTX 4000 Ada listed with 16 vCPU had a 13.6-core quota (EPYC 7702); an RTX 3090 Secure, 31.1.
- `runpodctl` cannot ask for cores; the GraphQL API can (`podFindAndDeployOnDemand(input: {minVcpuCount, minMemoryInGb,
  supportPublicIp})`, `cloud/runpod/deploy.py`). The launcher reads the real quota over SSH (`/sys/fs/cgroup/cpu.max`,
  or v1's `cpu.cfs_quota_us`) and rejects a pod below 75% of the listing before copying anything (the rejected pod
  cost $0.02).

## Encoder sessions, video memory, and the page thread

- GeForce drivers cap NVENC at 8 concurrent sessions (seen on RTX 3090 and 4090); the RTX 4000 Ada opened 16. The job
  measures it at setup (16 parallel 4K encodes) and caps the workers.
- A100, H100 and B200 have no NVENC at all.
- On the RTX 4000 Ada (20 GB): 1 worker 3.0 frames/s (GPU 10% busy), 4 workers 6.6, 11 workers 15.4 steady (GPU 34% on
  light shots, 100% on the heaviest; CPU 6.9 of 13.6 cores; 16.2 GB of video memory, about 1.45 GB a worker, so 11-12
  workers at most). The per-worker limit was the page thread composing a 4K frame (0.2 to 0.8 s by shot) and the
  readback queue on the GPU (35 ms a frame alone, 53-117 ms with 11 workers).
- On the RTX 3090 (31 cores, 8 workers) the rate stayed near 9 frames/s: each worker waited longer per frame as workers
  were added (0.4 s at 4 workers, 0.9 s at 8), and the CPU was only 5.6 cores busy.

## Jobs, queues and tails

- Jobs of 2,000 frames left a 20-minute idle tail at the end of a render (the last jobs ran on a few workers while the
  rest waited). Jobs of 500 frames fixed it.
- A single heavy chunk (a split-screen shot traced twice, 0.63 frames/s) took 53 minutes and set the finish time of a
  whole run. Heavy ranges now go first, in job-sized pieces (`HEAVY` in the job file).
- Workers claim jobs with `mkdir` (atomic: only one can win). Two workers once rendered the same chunk and raced on a
  segment's rename; claims ended that.

## Pitfalls met (all handled in the scripts)

- `runpodctl`'s log stream lags by minutes: waiting for "ready" in it wasted time, and a stale "results ready" line once
  started a collector early. The launcher tests SSH directly and polls a file over SSH.
- The results collector died when the pod deleted itself mid-copy: the pod now waits (30 minutes by default) for the
  launcher to say it has collected.
- A bare `wait` in the job also waited for the watchdog (hours): wait for specific PIDs.
- `cmd | grep -q` under `set -o pipefail` fails when grep exits early; `set -e` aborted a delete-retry loop when one
  delete call failed. Neither pattern is used.
- ffmpeg reads stdin: in a script piped to bash it ate the rest of the script (`-nostdin`).
- `pkill -f` matched the SSH session that ran it.
- macOS `tar` adds metadata files (`COPYFILE_DISABLE=1`, `--no-mac-metadata`; extract with `--no-same-owner`).
- Pods made through the API start SSH only with a `PUBLIC_KEY` in their environment; Community cloud pods may have no
  public IP (ask for `supportPublicIp`).
- NVENC fails with `unsupported device` in a container given one GPU of a multi-GPU host (NVIDIA drivers 565 and later,
  nvidia-container-toolkit#1249): `nvenc-gpu-filter/` filters the driver's GPU list to the mounted one.
- The container must expose the driver's graphics libraries (`NVIDIA_DRIVER_CAPABILITIES=all`), or Chromium falls back
  to software rendering; the job refuses to render then.
- HDR readback buffers were reused, so the page's garbage collector seldom ran and 4K Blobs piled up until uploads
  failed at frame 16: the driver collects garbage every four frames.
- ANGLE's OpenGL backend reads 4K frames back 2-4 times faster than its Vulkan backend on NVIDIA, but draws points and
  edges differently: never mix backends within one film (`WEBGL2VIDEO_GPU_PROFILE` pins one).

## YouTube from the pod

A 6.66 GB master went from the pod to YouTube in 84 seconds, as Private, with the credentials held as RunPod secrets.
Only the small files (checks, stills, logs) come back to your machine.

## Prices seen (Secure cloud, 18 October 2026, $/hour)

RTX 4000 Ada 0.28 (16+ vCPU) · RTX A4500 0.25 · A5000 0.27 · RTX 3090 0.50 (32+ vCPU) · L4 0.59 · A6000 0.59 ·
RTX 4090 0.89 · L40S 1.09 · RTX 5090 1.19. Community: RTX 3090 0.22 (24+ vCPU), RTX 3080 Ti 0.18, A5000 0.16,
RTX 4090 0.34. Disks add about $0.01/hour.
