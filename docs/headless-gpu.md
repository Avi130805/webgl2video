# Real GPU WebGL in headless Chrome on a Linux server

Headless Chromium on a Linux server or in Docker quietly falls back to SwiftShader, a CPU implementation of WebGL, when
it cannot reach the GPU. Nothing fails: the page renders, only on the CPU and much slower, and `nvidia-smi` and CUDA work
fine in the same container, which makes it confusing. Public threads where people hit this:
[Docker forums](https://forums.docker.com/t/hardware-acceleration-for-headless-chrome-with-nvidia-gpu/132557) (no
answer), [Fly.io community](https://community.fly.io/t/use-gpu-acceleration-on-puppeteer/25489) (solved with Vulkan
libraries and the driver's graphics capabilities, on L40S and A10 machines).

This is the recipe webgl2video's cloud job uses on RunPod's NVIDIA pods, where it rendered 4K60 films on the GPU.

## 1. Check first, and fail loudly

```bash
python -m webgl2video gpucheck
```

```
GPU  GPU, headless: ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version) (textures up to 16384, float targets yes)
hardware WebGL: yes (GPU, headless)
```

(on a Mac; on a Linux server the renderer string names the NVIDIA GPU and the Vulkan backend, and a software fallback
reads `SwiftShader Device` or `llvmpipe`)

It launches Chromium with each launch profile below, creates a WebGL2 context, prints the renderer
(`UNMASKED_RENDERER_WEBGL`), and **exits 1** when every profile got a software renderer (SwiftShader, llvmpipe,
lavapipe). Put it in your container's entrypoint or your CI. `render --require-gpu` and the cloud job refuse to render on
a software renderer for the same reason.

## 2. The container must expose the driver's graphics libraries

The NVIDIA container runtime mounts only the compute libraries unless asked for more:

```bash
docker run --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all ...
```

(or `NVIDIA_DRIVER_CAPABILITIES=graphics,video,compute,utility`; `video` is NVENC). Without `graphics`, Vulkan finds no
NVIDIA device and Chromium falls back to SwiftShader. On RunPod, `cloud/runpod/launch.sh` sets it in the pod's
environment.

## 3. Vulkan and EGL in the image

```bash
apt-get install -y libvulkan1 vulkan-tools libegl1 libgl1
vulkaninfo --summary | grep -i nvidia        # the NVIDIA device must be listed
```

The Fly.io thread also needed `libdrm2`, `libgbm1` and `mesa-utils` (for EGL) on its image, and `ldconfig` in the
entrypoint. Run checks like `nvidia-smi` in the entrypoint, not in the Dockerfile: the image builder has no GPU.

## 4. Chromium's flags

ANGLE on Vulkan, headless, with the GPU blocklist ignored and no window surface:

```
--enable-gpu --ignore-gpu-blocklist --use-angle=vulkan --enable-features=Vulkan --disable-vulkan-surface
--no-sandbox --disable-dev-shm-usage
```

then, if that fails, ANGLE on EGL: `--enable-gpu --ignore-gpu-blocklist --use-gl=angle --use-angle=gl-egl`. These are
the first two profiles in `webgl2video/browser.py`; `WEBGL2VIDEO_GPU_PROFILE` pins one. Do not mix the two within one
film: ANGLE's OpenGL backend read 4K frames back 2-4 times faster on NVIDIA, but drew points and edges differently.

## 5. A GPU that renders

Pick a GPU with graphics and, for video, NVENC: workstation and gaming cards (RTX 4000 Ada, A4500, A5000, RTX 3090,
4090) and graphics data-centre cards (L4, L40S, A10). A100, H100 and B200 have no NVENC. GeForce drivers cap NVENC at 8
concurrent sessions. On a multi-GPU host with NVIDIA's 570-580 drivers, NVENC needs
[nvenc-gpu-filter](../nvenc-gpu-filter/README.md).

## On a Mac

Chromium's headless mode uses the GPU through Metal (`ANGLE Metal Renderer: Apple M2`, as `gpucheck` shows). If it does
not, webgl2video falls back to a small visible window, which always gets the GPU on macOS.
