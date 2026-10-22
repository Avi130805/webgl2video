#!/bin/bash
# A cloud render, on the pod. cloud/runpod/launch.sh creates the pod, sends the bundle and starts this; from then on it
# runs by itself, and everything it says goes to the pod's log and to $WORKSPACE/logs/job.log.
#
#   1. setup: the machine (GPU, CPU quota, video memory, NVENC sessions), ffmpeg, Chromium, the NVENC shim
#   2. preflight: hardware WebGL for the page, its frame count, the page's GPU conversion checked exact on this GPU at
#      the render size (else ffmpeg converts)
#   3. the scaling test: real jobs of the film rendered 1, then 4, then N at a time (N: what the CPU quota, the video
#      memory and the NVENC sessions allow), measuring frames per second, the GPU, PCIe, the encoder and where the CPU
#      goes, then projecting minutes and dollars for each tested worker count
#   4. the decision: "go <workers>" or "stop" in $WORKSPACE/DECISION (pod.sh <run> decide "go 11"); with BUDGET
#      (dollars), the fastest tested count whose projection fits goes by itself (on a near tie, the most workers). No decision in DECIDE_MINUTES: the pod
#      stops (it is not deleted) and its volume keeps every finished segment
#   5. the rest of the film, NW workers on one queue (each job claimed with mkdir, which only one worker can win), each
#      job checked as it lands (python -m webgl2video qa: unexpected cuts, single-frame flashes, stills)
#   6. the join, the checks (frames, loudness, sync, stills), the upload (optional, Private), the results to the
#      launcher (RESULTS_READY, then COLLECTED), and the pod deletes itself
#
# The pod is deleted when the job ends (KEEP_POD=1 keeps it), when MAX_SECONDS have passed (a watchdog that outlives
# this script: the cost cap, work and all), when this script dies before rendering began (an EXIT trap), or by the
# launcher if anything fails before the job starts. Once rendering has begun (on this volume, in this run or an earlier
# one), a failure stops the pod instead: its volume keeps the work, and rerunning this script on it resumes (a decision
# made earlier is kept in $WORKSPACE/DECIDED). A master nobody collected (no upload, no launcher) also stops the pod.
#
#   bash job.sh                 the job (on a pod, started by launch.sh)
#   bash job.sh add-worker K    one more worker on the running job's queue (pod.sh <run> add-worker K)
#
# Settings: job.env in the bundle (job.env.example), and from the launcher, in the pod's environment: RF_PRICE ($/h),
# RF_BUDGET, RF_MAX_SECONDS, RF_DECIDE_MINUTES, RF_KEEP_POD, RF_YT_UPLOAD, RF_COLLECT_MINUTES, and the YouTube
# credentials as RunPod secrets (never printed). A dry run on any machine, no pod and no NVIDIA:
#   WORKSPACE=<dir> SKIP_SETUP=1 ENCODER=x264 bash cloud/runpod/job.sh          (tests/test_cloud.py does this)
# Bash 3.2 or later (the dry run works with macOS's own bash).
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FARM=$(cd "$HERE/../.." && pwd) # the bundle: webgl2video/, cloud/, nvenc-gpu-filter/, site/, job.env
MODE=${1:-job}

# On a pod this script is started over SSH, which does not inherit the container's environment (RunPod's secrets and
# the launcher's settings live in PID 1's): take it from there. Nothing here prints it.
PID1_ENV=$( { tr '\0' '\n' < /proc/1/environ; } 2>/dev/null || true)
case "$PID1_ENV" in
  *RUNPOD_POD_ID=*)
    eval "$(python3 -c 'import shlex
for x in open("/proc/1/environ", "rb").read().split(b"\0"):
    k, _, v = x.decode("utf-8", "replace").partition("=")
    if k.isidentifier() and _: print("export " + k + "=" + shlex.quote(v))')";;
esac
unset PID1_ENV

W=${WORKSPACE:-/workspace}
JOB_ENV=${JOB_ENV:-$FARM/job.env}
[ -f "$JOB_ENV" ] && . "$JOB_ENV"
: "${PAGE:?job.env names the page: PAGE=...}"
SITE=$FARM/site
SIZE=${SIZE:-3840x2160}; SCALE=${SCALE:-1}; HDR=${HDR:-0}; ENCODER=${ENCODER:-nvenc}; QUALITY=${QUALITY:-}
PARAMS=${PARAMS:-}; AUDIO=${AUDIO:-auto}; NAME=${NAME:-film}
JOB_FRAMES=${JOB_FRAMES:-500}; SEGMENT=${SEGMENT:-$JOB_FRAMES}; HEAVY=${HEAVY:-}; CUTS=${CUTS:-}; STARTS=${STARTS:-}
STILLS_FULL=${STILLS_FULL:-}
MAX_WORKERS=${MAX_WORKERS:-16}; CORES_PER_WORKER=${CORES_PER_WORKER:-1.1}; VRAM_PER_WORKER_GB=${VRAM_PER_WORKER_GB:-1.6}
YT_TITLE=${YT_TITLE:-$NAME}; YT_PRIVACY=${YT_PRIVACY:-private}; YT_DESCRIPTION=${YT_DESCRIPTION:-}
PRICE=${RF_PRICE:-${PRICE:-0}}; BUDGET=${RF_BUDGET:-${BUDGET:-}}; MAX_SECONDS=${RF_MAX_SECONDS:-${MAX_SECONDS:-14400}}
DECIDE_MINUTES=${RF_DECIDE_MINUTES:-${DECIDE_MINUTES:-20}}; KEEP_POD=${RF_KEEP_POD:-${KEEP_POD:-0}}
YT_UPLOAD=${RF_YT_UPLOAD:-${YT_UPLOAD:-0}}; COLLECT_MINUTES=${RF_COLLECT_MINUTES:-${COLLECT_MINUTES:-}}
SKIP_SETUP=${SKIP_SETUP:-0}; ALLOW_CPU=${ALLOW_CPU:-0}
ON_POD=0; [ -n "${RUNPOD_POD_ID:-}" ] && ON_POD=1
WORK=$W/work; OUT=$W/out; DELIVER=$W/deliver
export PYTHONPATH="$FARM${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$W/logs"
T0=$(date +%s)
LOG_TAG=
log() { echo "[$(date +%H:%M:%S) +$(( $(date +%s) - T0 ))s$LOG_TAG] $*"; } # local time, as the driver logs (UTC on a pod)

FINISHED=0; RENDERING=0; WATCHDOG=
delete_pod() {
  if [ "$ON_POD" != 1 ]; then log "(not on a RunPod pod: nothing to delete)"; return 0; fi
  runpodctl pod delete "$RUNPOD_POD_ID" >/dev/null 2>&1 || runpodctl remove pod "$RUNPOD_POD_ID" >/dev/null 2>&1 \
    || log "could not delete this pod from inside: delete it from your machine (cloud/runpod/pod.sh <run> delete)"
}
stop_pod() {
  if [ "$ON_POD" != 1 ]; then log "(not on a RunPod pod: nothing to stop)"; return 0; fi
  runpodctl pod stop "$RUNPOD_POD_ID" >/dev/null 2>&1 || runpodctl stop pod "$RUNPOD_POD_ID" >/dev/null 2>&1 \
    || log "could not stop this pod from inside: stop or delete it from your machine (cloud/runpod/pod.sh <run> delete)"
}
send_results() {  # the launcher copies deliver/ over SSH when it sees RESULTS_READY, then touches COLLECTED
  mkdir -p "$DELIVER/logs"
  cp "$W"/logs/*.log "$W"/logs/*.txt "$W"/logs/*.json "$DELIVER/logs/" 2>/dev/null
  touch "$W/RESULTS_READY" # polled over SSH (the pod's log stream lags minutes)
  log "RESULTS ready: $(du -sh "$DELIVER" | cut -f1) in $DELIVER"
  local wait_min=${COLLECT_MINUTES:-30} i
  [ "$ON_POD" = 1 ] || wait_min=${COLLECT_MINUTES:-0}
  COLLECTED_OK=0
  for ((i = 0; i < wait_min * 6; i++)); do
    [ -e "$W/COLLECTED" ] && { log "RESULTS collected"; COLLECTED_OK=1; return 0; }
    sleep 10
  done
  [ "$wait_min" = 0 ] || log "RESULTS not collected in $wait_min min"
}
master_waiting() {  # a master in deliver/ that nobody has collected (no upload, no launcher)
  local f
  [ "$ON_POD" = 1 ] && [ "${COLLECTED_OK:-0}" != 1 ] || return 1
  for f in "$DELIVER"/*.mp4 "$DELIVER"/*.mkv; do [ -e "$f" ] && return 0; done
  return 1
}
quiet_watchdog() { if [ -n "$WATCHDOG" ]; then pkill -P "$WATCHDOG" 2>/dev/null; kill "$WATCHDOG" 2>/dev/null; fi; }
finish() {
  FINISHED=1
  log "PHASE end: exit $1, $(( ($(date +%s) - T0) / 60 )) min in all"
  send_results
  if [ "$KEEP_POD" = 1 ]; then
    log "KEEP_POD=1: the pod stays up (and bills) until you delete it (pod.sh <run> delete) or MAX_SECONDS pass"
    if [ "$ON_POD" = 1 ]; then sleep infinity; fi
    quiet_watchdog; exit "$1"
  fi
  if master_waiting; then
    log "ALL DONE ($1), but nobody collected the master: stopping this pod instead of deleting it. Start it, then"
    log "cloud/runpod/pod.sh <run> collect, then pod.sh <run> delete (a stopped pod bills for its volume until then)"
    stop_pod
    if [ "$ON_POD" = 1 ]; then sleep 900; fi
    quiet_watchdog; exit "$1"
  fi
  log "ALL DONE ($1): deleting this pod"
  delete_pod
  if [ "$ON_POD" = 1 ]; then sleep 900; fi
  quiet_watchdog; exit "$1"
}
die() { log "FAILED: $*"; finish 1; }
halt_keep() {  # rendered work exists: stop paying for the GPU, keep the volume (and the work)
  FINISHED=1
  log "STOPPING (work kept): $*"
  send_results
  log "stopping this pod (not deleting it): its volume keeps every finished segment; start it and rerun the job to resume"
  stop_pod
  if [ "$ON_POD" = 1 ]; then sleep 900; fi
  quiet_watchdog; exit 2
}
on_exit() {  # the script ended some other way (a bug, a kill): never leave a pod running
  local code=$?
  [ "$FINISHED" = 1 ] && return
  FINISHED=1
  log "the job ended unexpectedly (exit $code)"
  send_results
  if [ "$RENDERING" = 1 ]; then log "stopping the pod, keeping its work"; stop_pod
  else log "deleting the pod"; delete_pod; fi
  quiet_watchdog
}

cpu_quota() {  # the cores this container may use (cgroup v2, then v1), not the host's count
  local q p
  if [ -r /sys/fs/cgroup/cpu.max ]; then read -r q p < /sys/fs/cgroup/cpu.max
  elif [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
    q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)
  else q=max; fi
  case "$q" in max|-1|"") getconf _NPROCESSORS_ONLN;; *) awk -v q="$q" -v p="$p" 'BEGIN { printf "%.1f", q / p }';; esac
}

now() { python3 -c 'import time; print(f"{time.time():.3f}")'; } # sub-second (macOS date has no %N)
load_jobs() { JOBS=(); while IFS= read -r l; do [ -n "$l" ] && JOBS+=("$l"); done < "$W/queue.txt"; }
frames_done() {  # the frames of the jobs marked done (a job rendered again on a resumed run counts once)
  local i j n=0
  for ((i = 0; i < ${#JOBS[@]}; i++)); do
    if [ -e "$W/done/$i" ]; then j=${JOBS[$i]}; n=$((n + ${j#*:} - ${j%%:*})); fi
  done
  echo "$n"
}

render_cmd() {  # the driver, as every job and the join run it
  RENDER=("$PY" -m webgl2video render "$SITE/$PAGE" --root "$SITE" --size "$SIZE" --scale "$SCALE" --encoder "$ENCODER"
          --segment "$SEGMENT" --work "$WORK" --convert "$CONVERT")
  if [ -n "$QUALITY" ]; then RENDER+=(--quality "$QUALITY"); fi
  if [ "$HDR" = 1 ]; then RENDER+=(--hdr); fi
  if [ "$ALLOW_CPU" != 1 ]; then RENDER+=(--require-gpu); fi
  local kv
  for kv in $PARAMS; do RENDER+=(--param "$kv"); done
}

job() {  # $1 = worker, $2 = queue index: that job rendered, then checked; 1 on a failure
  local k=$1 idx=$2 j a b n t lg dt res
  j=${JOBS[$idx]}; a=${j%%:*}; b=${j#*:}; n=$((b - a)); t=$(date +%s); lg="$W/logs/job-$a-$b.log"
  "${RENDER[@]}" --frames "$j" --segments-only > "$lg" 2>&1 \
    || { log "JOB $j FAILED (worker $k): $(tail -2 "$lg" | tr '\n' ' ')"; return 1; }
  dt=$(( $(date +%s) - t )); touch "$W/done/$idx"
  local qa=("$PY" -m webgl2video qa "$WORK" "$a" "$b" "$DELIVER/checks")
  if [ -n "$CUTS" ]; then qa+=(--cuts "$CUTS"); fi
  if [ -n "$STARTS" ]; then qa+=(--starts "$STARTS"); fi
  "${qa[@]}" > "$W/logs/check-$a-$b.log" 2>&1 || log "CHECK $j: the checker failed ($(tail -1 "$W/logs/check-$a-$b.log"))"
  res=$(tail -1 "$W/logs/check-$a-$b.log")
  log "JOB $j done in ${dt}s by worker $k: $(awk -v n="$n" -v d="$dt" 'BEGIN { printf "%.2f", n / (d > 0 ? d : 1) }') frames/s;" \
      "check: $("$PY" -c 'import json, sys
try:
    r = json.loads(sys.argv[1]); print("max diff", r["max"], "known cuts", [c["frame"] for c in r["known_cuts"]], "flags", r["flags"] or "none")
except Exception:
    print("no result")' "$res")"
  if [ -n "${RENDER_T0:-}" ]; then
    local total el
    total=$(frames_done); el=$(( $(date +%s) - RENDER_T0 ))
    log "PROGRESS $total of $FRAMES frames; $(awk -v d="$((total - DONE_AT_GO))" -v e="$el" -v left="$((FRAMES - total))" \
      'BEGIN { r = d / (e > 0 ? e : 1); printf "%.2f frames/s since the go; about %d min left", r, (r > 0 ? left / r / 60 : 0) }')"
  fi
}

worker() {  # $1 = worker number: the next unclaimed job, until none is left
  local k=$1 i
  for ((i = 0; i < ${#JOBS[@]}; i++)); do
    [ -e "$W/STOP" ] && return 1
    [ -e "$W/done/$i" ] && continue
    mkdir "$W/claim/$i" 2>/dev/null || continue
    job "$k" "$i" || { touch "$W/STOP"; return 1; }
  done
}

samplers_start() {  # $1 = tag: the GPU (nvidia-smi dmon) and where the CPU goes, while a stage renders
  DMON=; CPUS=
  if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi dmon -s pucvmt -d "${2:-2}" > "$W/logs/dmon-$1.txt" 2>/dev/null & DMON=$!; fi
  if [ -r /proc/self/stat ]; then "$PY" "$HERE/cpu_sample.py" "$W/logs/cpu-$1.txt" & CPUS=$!; fi
}
samplers_stop() {
  if [ -n "$CPUS" ]; then kill "$CPUS" 2>/dev/null; fi
  if [ -n "$DMON" ]; then kill "$DMON" 2>/dev/null; fi
  sleep 3
}

# ---- one more worker on a running job ---------------------------------------------------------------------------------
if [ "$MODE" = add-worker ]; then
  K=${2:-99}; LOG_TAG=" worker $K"
  exec > >(tee -a "$W/logs/job.log") 2>&1
  [ -s "$W/run.env" ] || { log "no running job here ($W/run.env is missing)"; exit 1; }
  . "$W/run.env" # PY, CONVERT, FRAMES: as the job's preflight found them
  if [ -f "$W/venv/bin/activate" ]; then . "$W/venv/bin/activate"; fi
  [ -s "$W/DECIDED" ] || { log "the job has not decided to go yet (the scaling test comes first): add workers after it"; exit 1; }
  load_jobs; render_cmd
  mkdir -p "$W/workers"; echo $$ > "$W/workers/$$"; trap 'rm -f "$W/workers/$$"' EXIT # the join waits for it
  log "joins: ${#JOBS[@]} jobs in the queue, $(ls "$W/claim" | wc -l | tr -d ' ') already claimed"
  worker "$K"
  log "done: no job left to claim"
  exit 0
fi
[ "$MODE" = job ] || { echo "usage: job.sh [add-worker K]"; exit 2; }

# ---- the job ------------------------------------------------------------------------------------------------------------
exec > >(tee -a "$W/logs/job.log") 2>&1
trap on_exit EXIT
# a restarted pod runs this again: finished segments, the queue and an earlier decision stay; claims start fresh
rm -rf "$W/claim" "$W/workers" "$W/STOP" "$W/COLLECTED" "$W/RESULTS_READY"
mkdir -p "$W/claim" "$W/done" "$W/workers" "$WORK" "$OUT" "$DELIVER/checks" "$DELIVER/stills" "$DELIVER/ladder"
if [ -s "$W/DECIDED" ] || [ -n "$(ls -A "$W/done" 2>/dev/null)" ]; then RENDERING=1; fi # a resumed run: the work is here
if [ ! -s "$W/DECIDED" ] && [ -e "$W/DECISION" ]; then mv "$W/DECISION" "$W/DECISION.prev"; fi # an earlier run's "stop"
if [ "$ON_POD" = 1 ] && command -v setsid >/dev/null 2>&1; then # the watchdog outlives this script
  setsid bash -c "sleep $MAX_SECONDS; echo '[watchdog] MAX_SECONDS ($MAX_SECONDS) reached: deleting the pod';
    runpodctl pod delete $RUNPOD_POD_ID >/dev/null 2>&1 || runpodctl remove pod $RUNPOD_POD_ID" > /proc/1/fd/1 2>&1 < /dev/null &
else
  ( sleep "$MAX_SECONDS"; log "MAX_SECONDS ($MAX_SECONDS) reached: deleting the pod"; delete_pod ) 2>/dev/null & WATCHDOG=$!
fi
log "job: $PAGE at $SIZE (x$SCALE), HDR $HDR, $ENCODER${QUALITY:+ q$QUALITY}, jobs of $JOB_FRAMES frames; price \$$PRICE/h;" \
    "budget ${BUDGET:-none}; cap ${MAX_SECONDS}s"

# ---- 1. the machine -----------------------------------------------------------------------------------------------------
log "PHASE setup"
case "$AUDIO" in
  auto|none|page) AUDIO_ARG=$AUDIO;;
  *) AUDIO_ARG="$FARM/$AUDIO"; [ -f "$AUDIO_ARG" ] || die "AUDIO=$AUDIO: no such file in the bundle";;
esac
[ $((JOB_FRAMES % SEGMENT)) = 0 ] || die "SEGMENT ($SEGMENT) must divide JOB_FRAMES ($JOB_FRAMES)"
if [ "$SKIP_SETUP" = 1 ]; then
  PY=${PYTHON:-python3}; CORES=$(cpu_quota); VRAM=0; NVENC=$MAX_WORKERS
  log "setup skipped (SKIP_SETUP=1): $CORES cores; $("$PY" --version 2>&1)"
else
  GPU=$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader) || die "no NVIDIA GPU visible"
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
  CORES=$(cpu_quota)
  log "machine: $GPU; CPU quota $CORES cores (host $(getconf _NPROCESSORS_ONLN), $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)); RAM $(free -g | awk '/Mem/ {print $2}') GB"
  export DEBIAN_FRONTEND=noninteractive
  EXTRA_PKGS=""; if [ "$HDR" = 1 ]; then EXTRA_PKGS=mkvtoolnix; fi
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends ffmpeg libvulkan1 vulkan-tools libegl1 \
    libgl1 python3-venv ca-certificates gcc libc6-dev pciutils $EXTRA_PKGS >/dev/null || die "apt-get failed"
  vulkaninfo --summary > "$W/logs/vulkan.txt" 2>/dev/null || true # (grep -q in a pipe trips pipefail: read a file)
  grep -qi nvidia "$W/logs/vulkan.txt" \
    || die "Vulkan sees no NVIDIA GPU (the container needs NVIDIA_DRIVER_CAPABILITIES=all): stopping before any render"
  nvidia-smi -q > "$W/logs/nvidia-smi-q.txt" 2>/dev/null || true
  log "PCIe: $(grep -A3 'GPU Link Info' "$W/logs/nvidia-smi-q.txt" | grep -E 'Max|Current' | head -4 | tr -s ' ' | tr '\n' ' ')"
  log "clocks: $(nvidia-smi --query-gpu=clocks.max.graphics,clocks.max.memory,power.limit --format=csv,noheader)"
  # NVENC on a multi-GPU host needs the GPU-filter shim (nvenc-gpu-filter/), loaded into every ffmpeg by a wrapper
  export PATH="/usr/local/bin:$PATH"
  if gcc -O2 -shared -fPIC -o /opt/nvenc-gpu-filter.so "$FARM/nvenc-gpu-filter/nvenc-gpu-filter.c" -ldl -lpthread 2> "$W/logs/shim.txt"; then
    printf '#!/bin/sh\nLD_PRELOAD=/opt/nvenc-gpu-filter.so${LD_PRELOAD:+:$LD_PRELOAD} exec /usr/bin/ffmpeg "$@"\n' > /usr/local/bin/ffmpeg
    chmod +x /usr/local/bin/ffmpeg; hash -r
  fi
  python3 -m venv "$W/venv" && . "$W/venv/bin/activate" || die "venv failed"
  pip install -q numpy playwright==1.56.0 || die "pip failed"
  python3 -m playwright install --with-deps chromium >/dev/null 2>&1 || die "playwright's Chromium failed to install"
  PY=python3
  if [ "$HDR" = 1 ]; then NV=(-vf format=p010le -c:v hevc_nvenc -profile:v main10); else NV=(-vf format=yuv420p -c:v h264_nvenc); fi
  if [ "$ENCODER" = nvenc ]; then
    ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i color=black:s=3840x2160:r=60 -frames:v 30 "${NV[@]}" -preset p6 \
      -f null - > "$W/logs/nvenc.txt" 2>&1 || die "NVENC won't open: $(grep -v nvenc-gpu-filter "$W/logs/nvenc.txt" | head -2 | tr '\n' ' ')"
    # how many NVENC sessions this card opens at once (GeForce drivers cap it; workstation cards don't): 16 at once, 4K
    # (wait for these PIDs only: a bare `wait` also waits for the watchdog, and the job sat there)
    rm -f "$W/nvenc-ok"; NP=()
    for k in $(seq 16); do
      ( ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i testsrc2=s=3840x2160:r=60 -frames:v 240 "${NV[@]}" -preset p6 \
          -f null - > "$W/logs/nvenc-$k.txt" 2>&1 && echo ok >> "$W/nvenc-ok" ) & NP+=($!)
    done
    wait "${NP[@]}"
    NVENC=$(grep -c ok "$W/nvenc-ok" 2>/dev/null || echo 0); rm -f "$W/nvenc-ok"
  else
    NVENC=$MAX_WORKERS
  fi
fi
# workers: CORES_PER_WORKER cores, VRAM_PER_WORKER_GB of video memory and one NVENC session each, MAX_WORKERS at most
NMAX=$(awk -v c="$CORES" -v v="$VRAM" -v e="$NVENC" -v cw="$CORES_PER_WORKER" -v vw="$VRAM_PER_WORKER_GB" -v mx="$MAX_WORKERS" \
  'BEGIN { w = int(c / cw); if (v > 0) { m = int((v / 1024 - 2) / vw); if (m < w) w = m }; if (e < w) w = e;
           if (w > mx) w = mx; if (w < 1) w = 1; print w }')
log "setup done: NVENC sessions at once: $NVENC; workers at most $NMAX"

# ---- 2. preflight: hardware WebGL, the page, the GPU conversion -----------------------------------------------------
log "PHASE preflight"
FLAGS=(--root "$SITE")
if [ "$HDR" = 1 ]; then FLAGS+=(--hdr); fi
for kv in $PARAMS; do FLAGS+=(--param "$kv"); done
if [ "$ALLOW_CPU" != 1 ]; then FLAGS+=(--require-gpu); fi
"$PY" -m webgl2video probe "$SITE/$PAGE" "${FLAGS[@]}" > "$W/logs/probe.json" 2> "$W/logs/probe.log" \
  || die "no hardware WebGL for the page here: $(tail -2 "$W/logs/probe.log" | tr '\n' ' ')"
FRAMES=$(python3 -c 'import json, sys; print(json.loads(open(sys.argv[1]).read().splitlines()[-1])["frames"])' "$W/logs/probe.json")
log "page: $(tail -1 "$W/logs/probe.json")"
RSIZE=$(python3 -c 'import sys; w, h = map(int, sys.argv[1].split("x")); s = float(sys.argv[2]); e = lambda x: int(round(x * s / 2)) * 2; print(f"{e(w)}x{e(h)}")' "$SIZE" "$SCALE")
EXACT_AT=$(python3 -c "n = $FRAMES; print(','.join(sorted({str(min(n - 1, int(n * k / 5))) for k in range(1, 5)}, key=int)))")
# The page's conversion (NV12, or P010 and the light levels) against a float64 reference made from its plain RGB
# frames, on this GPU, at the render size: within one code on a negligible share of samples, or ffmpeg converts.
if "$PY" -m webgl2video exact "$SITE/$PAGE" --size "$RSIZE" --frames "$EXACT_AT" "${FLAGS[@]}" > "$W/logs/exact.log" 2>&1; then
  CONVERT=gpu; log "GPU conversion: exact on this GPU ($RSIZE, frames $EXACT_AT); the render uses it"
else
  CONVERT=cpu; log "GPU conversion: NOT exact here (logs/exact.log); ffmpeg converts the frames instead"
fi
grep -E "^frame|EXACT" "$W/logs/exact.log" | head -20
if [ "$YT_UPLOAD" = 1 ]; then # the login, before hours of rendering depend on it
  "$PY" -m webgl2video upload --check > "$W/logs/upload-check.log" 2>&1 \
    || die "the YouTube login failed (the RunPod secrets yt_client_id, yt_client_secret, yt_refresh_token): $(tail -1 "$W/logs/upload-check.log")"
  log "YouTube: $(head -1 "$W/logs/upload-check.log")"
fi
printf 'PY=%q\nCONVERT=%q\nFRAMES=%q\n' "$PY" "$CONVERT" "$FRAMES" > "$W/run.env"
render_cmd

# ---- the queue: every job once (claimed with mkdir), the same across restarts ----------------------------------------
if [ ! -s "$W/queue.txt" ]; then
  python3 "$HERE/queue.py" "$FRAMES" "$NMAX" --job "$JOB_FRAMES" --heavy "$HEAVY" > "$W/queue.txt" 2> "$W/stages.txt" \
    || die "the queue: $(cat "$W/stages.txt")"
fi
load_jobs
read -r _ STAGES_LINE < "$W/stages.txt"; STAGE_N=($STAGES_LINE)
log "queue: ${#JOBS[@]} jobs of up to $JOB_FRAMES frames: ${JOBS[*]:0:8} ..."

# ---- 3. the scaling test, 4. the decision (skipped when a restarted pod resumes) -------------------------------------
RENDERING=1
if [ -s "$W/DECIDED" ]; then
  NW=$(cat "$W/DECIDED"); log "resuming with the decision made earlier: $NW workers"
else
  log "PHASE scaling test (real jobs of the film, kept; workers ${STAGE_N[*]})"
  q=0
  for ((s = 0; s < ${#STAGE_N[@]}; s++)); do
    n=${STAGE_N[$s]}; IDX=()
    for ((k = 0; k < n; k++)); do i=$((q + k)); [ -e "$W/done/$i" ] || IDX+=("$i"); done
    q=$((q + n))
    if [ ${#IDX[@]} = 0 ]; then log "STAGE s$((s + 1)): its jobs were done in an earlier run: skipped"; continue; fi
    tag="s$((s + 1))-w${#IDX[@]}"
    samplers_start "$tag" 2
    ST=$(now); PIDS=(); STAGE_JOBS=(); k=0
    for i in "${IDX[@]}"; do
      mkdir -p "$W/claim/$i"; STAGE_JOBS+=("${JOBS[$i]}")
      ( job "$k" "$i" || touch "$W/STOP" ) & PIDS+=($!); k=$((k + 1))
    done
    wait "${PIDS[@]}"
    ET=$(now); samplers_stop
    [ -e "$W/STOP" ] && halt_keep "a test job failed (see the JOB lines above)"
    "$PY" "$HERE/ladder.py" stage "$W" "$tag" "$ST" "$ET" "${STAGE_JOBS[@]}" > "$W/logs/stage-$tag.log" 2>&1
    while read -r line; do log "$line"; done < "$W/logs/stage-$tag.log"
  done
  cp "$W"/logs/stage-*.txt "$DELIVER/ladder/" 2>/dev/null; cp "$W"/logs/dmon-*.txt "$W"/logs/cpu-*.txt "$DELIVER/ladder/" 2>/dev/null
  PROJ=("$PY" "$HERE/ladder.py" project "$W" "$FRAMES" "$(frames_done)" "$PRICE" "$T0" "${#JOBS[@]}" "$q")
  if [ -n "$BUDGET" ]; then PROJ+=(--budget "$BUDGET"); fi
  "${PROJ[@]}" > "$W/logs/projection.txt" 2>&1
  while read -r line; do log "$line"; done < "$W/logs/projection.txt"

  DEC=""
  if [ -s "$W/DECISION" ]; then DEC=$(cat "$W/DECISION")
  elif [ -n "$BUDGET" ]; then DEC=$(grep -E '^AUTO ' "$W/logs/projection.txt" | tail -1 | cut -d' ' -f2-)
  else
    log "DECISION wanted: write 'go <workers>' or 'stop' to $W/DECISION (cloud/runpod/pod.sh <run> decide 'go $NMAX');" \
        "waiting up to $DECIDE_MINUTES min, then stopping with the work kept"
    for ((i = 0; i < DECIDE_MINUTES * 6; i++)); do [ -s "$W/DECISION" ] && { DEC=$(cat "$W/DECISION"); break; }; sleep 10; done
  fi
  log "DECISION: ${DEC:-none}"
  case "$DEC" in
    go\ *) NW=${DEC#go }; [[ "$NW" =~ ^[0-9]+$ ]] && [ "$NW" -ge 1 ] && [ "$NW" -le "$MAX_WORKERS" ] \
             || halt_keep "a decision I can't read: $DEC (go 1 to go $MAX_WORKERS, or stop)";;
    stop*) halt_keep "the decision was to stop: ${DEC#stop}";;
    *) halt_keep "no decision in $DECIDE_MINUTES min";;
  esac
  echo "$NW" > "$W/DECIDED"
fi

# ---- 5. everything else, NW at a time -----------------------------------------------------------------------------------
log "PHASE render (${#JOBS[@]} jobs, $(ls "$W/done" | wc -l | tr -d ' ') done; $NW workers)"
RENDER_T0=$(date +%s); DONE_AT_GO=$(frames_done)
samplers_start render 10
PIDS=(); for ((k = 0; k < NW; k++)); do worker "$k" & PIDS+=($!); sleep 5; done
wait "${PIDS[@]}"
# workers added while it ran (add-worker) may still be on their last job: the join waits for them
while :; do
  alive=0
  for f in "$W"/workers/*; do
    [ -e "$f" ] || continue
    if kill -0 "$(basename "$f")" 2>/dev/null; then alive=1; else rm -f "$f"; fi
  done
  [ "$alive" = 0 ] && break
  sleep 10
done
samplers_stop; if [ -s "$W/logs/cpu-render.txt" ]; then log "render $(cat "$W/logs/cpu-render.txt")"; fi
[ -e "$W/STOP" ] && halt_keep "a job failed (see the JOB lines above)"
NDONE=$(ls "$W/done" | wc -l | tr -d ' ')
[ "$NDONE" -ge "${#JOBS[@]}" ] || halt_keep "only $NDONE of ${#JOBS[@]} jobs are done"
log "render done: $FRAMES frames, $(( ($(date +%s) - RENDER_T0) / 60 )) min after the go"

# ---- 6. the join, the checks, the upload --------------------------------------------------------------------------------
log "PHASE join"
EXT=mp4; if [ "$HDR" = 1 ]; then EXT=mkv; fi
M="$OUT/$NAME.$EXT"
"${RENDER[@]}" --out "$M" --keep --audio "$AUDIO_ARG" > "$W/logs/join.log" 2>&1 \
  || { tail -5 "$W/logs/join.log"; halt_keep "the join failed (logs/join.log)"; }
[ -s "$M" ] || halt_keep "the join wrote no master"
grep -E "Resuming|saved|HDR10" "$W/logs/join.log" | head -4
SYNC=""
case "$AUDIO_ARG" in auto|page) SYNC="$WORK/audio-page.wav";; none) ;; *) SYNC="$AUDIO_ARG";; esac

log "PHASE verify"
V=("$PY" -m webgl2video verify "$M" --expect-frames "$FRAMES" --stills "$DELIVER/stills")
if [ -n "$SYNC" ] && [ -f "$SYNC" ]; then V+=(--audio "$SYNC"); fi
if [ -n "$STILLS_FULL" ]; then V+=(--full "$STILLS_FULL"); fi
"${V[@]}" > "$W/logs/verify.log" 2>&1; VC=$?
grep -E "^(PROBE|LOUDNESS|SYNC|STILLS|FRAMES)" "$W/logs/verify.log" | cut -c1-400 | while read -r line; do log "$line"; done
[ "$VC" = 0 ] || halt_keep "the master failed its checks (logs/verify.log)"

if [ "$YT_UPLOAD" = 1 ]; then
  log "PHASE upload"
  U=("$PY" -m webgl2video upload --file "$M" --title "$YT_TITLE" --privacy "$YT_PRIVACY")
  if [ -n "$YT_DESCRIPTION" ] && [ -f "$FARM/$YT_DESCRIPTION" ]; then U+=(--description-file "$FARM/$YT_DESCRIPTION"); fi
  "${U[@]}" > "$W/logs/upload.log" 2>&1
  grep -v "^  " "$W/logs/upload.log" | while read -r line; do log "$line"; done
  grep -q "youtu.be/" "$W/logs/upload.log" || halt_keep "the upload failed (logs/upload.log)"
else
  mv "$M" "$DELIVER/" && log "no upload asked: the master goes back with the results ($(du -h "$DELIVER/$NAME.$EXT" | cut -f1))"
  if [ "$ON_POD" = 1 ]; then COLLECT_MINUTES=${COLLECT_MINUTES:-120}; fi # a large file over SSH
fi
finish 0
