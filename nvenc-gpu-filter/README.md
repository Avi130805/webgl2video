# nvenc-gpu-filter

An `LD_PRELOAD` shim that makes NVENC work in a container that was given **one GPU of a multi-GPU host**, with NVIDIA's
570, 575 and 580 drivers. One C file, no dependencies, fail-safe. Part of [webgl2video](../README.md), usable on its
own.

## The bug

In a container given one GPU of several (a Kubernetes pod with `nvidia.com/gpu: 1`, `docker run --gpus device=N`, a
cloud GPU pod on a shared host), ffmpeg's NVENC encoders fail unless the GPU happens to be the host's last one:

```
[h264_nvenc] OpenEncodeSessionEx failed: unsupported device (2): (no details)
[h264_nvenc] No capable devices found
```

Reported as [NVIDIA/nvidia-container-toolkit#1249](https://github.com/NVIDIA/nvidia-container-toolkit/issues/1249)
("NVENC Fails in Kubernetes Pods on all but the last GPU with Driver 570.x or 580.x"), which was closed on
6 August 2026 as not belonging in the toolkit, pointing to
[NVIDIA/k8s-device-plugin#1282](https://github.com/NVIDIA/k8s-device-plugin/issues/1282). Users there report the
550 and 565 drivers working, the 570, 575 and 580 series failing. Unreal Engine's encoder and CUDA decoding are reported
to fail the same way.

**Why:** the container still gets the host's whole list of GPUs from the driver's resource manager (the RM control
`NV0000_CTRL_CMD_GPU_GET_ATTACHED_IDS`). NVENC reaches for GPUs whose `/dev/nvidiaN` device nodes are not mounted in
the container, and gives up.

## The fix

The shim wraps `ioctl`. When the driver answers that one query, it looks up each GPU id in the answer (its PCI
location, asked of the driver with `NV0000_CTRL_CMD_GPU_GET_PCI_INFO`), and keeps only the GPUs whose
`/proc/driver/nvidia/gpus/<bus id>/information` names a device minor whose `/dev/nvidia<minor>` exists in this
container. Every other ioctl, and every other device, passes through untouched.

It is **fail-safe**: when anything cannot be resolved, or when nothing (or everything) would be kept, the driver's
answer is returned exactly as it was. With every GPU mounted, or on a single-GPU host, it changes nothing.

## Use

```bash
gcc -O2 -shared -fPIC -o nvenc-gpu-filter.so nvenc-gpu-filter.c -ldl -lpthread
LD_PRELOAD=$PWD/nvenc-gpu-filter.so ffmpeg -i in.mp4 -c:v hevc_nvenc out.mp4
```

To load it into every ffmpeg in a container, wrap the binary (this is what webgl2video's cloud job does at setup):

```bash
printf '#!/bin/sh\nLD_PRELOAD=/opt/nvenc-gpu-filter.so${LD_PRELOAD:+:$LD_PRELOAD} exec /usr/bin/ffmpeg "$@"\n' > /usr/local/bin/ffmpeg
chmod +x /usr/local/bin/ffmpeg
```

`NVENC_FILTER_DEBUG=1` prints what it sees and decides, on stderr:

```
[nvenc-gpu-filter] GPU 0000:41:00.0, minor 1: mounted here
[nvenc-gpu-filter] attached GPU id 0x00004100 at 0000:41:00: kept
[nvenc-gpu-filter] answer filtered to 1 of 3 GPUs
```

## Test (no GPU needed)

The same file builds a test against a mock driver and a mock `/dev` and `/proc`, on any OS:

```bash
cc -DNVENC_FILTER_TEST -o nvenc-filter-test nvenc-gpu-filter.c -lpthread && ./nvenc-filter-test
```

It checks one, two and all GPUs mounted, the driver's transfer wrapper (`NV_ESC_IOCTL_XFER_CMD`), a refused PCI query,
unknown GPUs, a missing `/proc` listing, and that other controls and other devices pass through.

## Where it has run

On RunPod GPU pods (containers given one GPU of a multi-GPU host), under ffmpeg's `h264_nvenc` and `hevc_nvenc`,
rendering 4K60 films. It has not been run under Kubernetes by its authors: the mechanism is the same, but try it on one
node first. Linux and glibc only (the test builds anywhere). The constants and structure layouts it names are those of
NVIDIA's [open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules) headers (`nv-ioctl-numbers.h`,
`nvos.h`, `ctrl0000gpu.h`); no code is copied from them.

MIT license, as the rest of the repository.
