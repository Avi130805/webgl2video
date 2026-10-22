#!/bin/bash
# Runs on your machine. Builds the bundle a pod needs (this tool, your page's folder, the job's settings, the soundtrack)
# and, with --go, creates the RunPod pod that renders it by itself (cloud/runpod/job.sh): the first GPU of the
# candidate list RunPod can give, through the GraphQL API (deploy.py: a minimum of CPU cores, a public IP for SSH), its
# real CPU quota checked before anything is copied. Then it sends the bundle in checksummed pieces, starts the job,
# follows the pod's log and receives the small files at the end. Without --go it only builds the bundle: nothing is
# spent.
#
# The job runs a scaling test first, then waits for a decision (or decides by itself with --budget): see the SSH line
# saved in <run>/ssh.txt and cloud/runpod/pod.sh <run> decide "go 11".
#
#   cloud/runpod/launch.sh --site DIR --job job.env [--page REL] [--audio FILE] [--name NAME]       (a dry run)
#   cloud/runpod/launch.sh ... --go [--budget USD] [--upload] [--decide-minutes 20] [--max-seconds 14400] [--keep-pod]
#        [--candidates "NVIDIA RTX 4000 Ada Generation|SECURE|16|0.28;NVIDIA RTX A4500|SECURE|12|0.25"]
#        [--ssh-key FILE] [--runs DIR] [--volume-gb 40] [--container-gb 30] [--bundle-out FILE]
#
# Candidates: "GPU type id|SECURE or COMMUNITY|minimum vCPUs|price $/h", tried in order. Needs runpodctl (configured
# with your API key) and an SSH key whose public half RunPod can install (runpodctl's own, by default).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FARM=$(cd "$HERE/../.." && pwd)
GO=0; UPLOAD=0; KEEP=0; BUDGET=""; DECIDE=20; MAXS=14400; SITE=""; JOBF=""; PAGE=""; AUDIO=""; NAME=""; BUNDLE_OUT=""
VOLUME=40; CONTAINER=30; RUNS="$HOME/.renderfarm/runs"; KEY="$HOME/.runpod/ssh/runpodctl-ssh-key"
CANDS="NVIDIA RTX 4000 Ada Generation|SECURE|16|0.28;NVIDIA RTX A4500|SECURE|12|0.25;NVIDIA GeForce RTX 3090|SECURE|24|0.50"
while [ $# -gt 0 ]; do case "$1" in
  --site) SITE="$2"; shift;; --job) JOBF="$2"; shift;; --page) PAGE="$2"; shift;; --audio) AUDIO="$2"; shift;;
  --name) NAME="$2"; shift;; --go) GO=1;; --upload) UPLOAD=1;; --keep-pod) KEEP=1;; --budget) BUDGET="$2"; shift;;
  --decide-minutes) DECIDE="$2"; shift;; --max-seconds) MAXS="$2"; shift;; --candidates) CANDS="$2"; shift;;
  --ssh-key) KEY="$2"; shift;; --runs) RUNS="$2"; shift;; --volume-gb) VOLUME="$2"; shift;;
  --container-gb) CONTAINER="$2"; shift;; --bundle-out) BUNDLE_OUT="$2"; shift;;
  -h|--help) sed -n '2,24p' "$0"; exit 0;;
  *) echo "unknown option $1 (--help)"; exit 2;; esac; shift; done
[ -n "$SITE" ] && [ -d "$SITE" ] || { echo "--site: the folder that holds your page (and everything it loads)"; exit 2; }
[ -n "$JOBF" ] && [ -f "$JOBF" ] || { echo "--job: the job's settings (cloud/runpod/job.env.example)"; exit 2; }
SITE=$(cd "$SITE" && pwd)
PAGE_IN_JOB=$( (. "$JOBF"; echo "${PAGE:-}") )
PAGE=${PAGE:-$PAGE_IN_JOB}
[ -n "$PAGE" ] && [ -f "$SITE/$PAGE" ] || { echo "the page '$PAGE' is not in $SITE (PAGE= in the job file, or --page)"; exit 2; }
[ -z "$AUDIO" ] || [ -f "$AUDIO" ] || { echo "--audio $AUDIO: no such file"; exit 2; }
NAME=${NAME:-$( (. "$JOBF"; echo "${NAME:-film}") )}
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--name: letters, digits, . _ - only"; exit 2; }
RUN="$RUNS/$NAME-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# ---- the bundle: only what the pod needs ---------------------------------------------------------------------------------
STAGE="$RUN/stage/farm"; mkdir -p "$STAGE"
ln -s "$FARM/renderfarm" "$STAGE/renderfarm"; ln -s "$FARM/cloud" "$STAGE/cloud"; ln -s "$SITE" "$STAGE/site"
{ cat "$JOBF"; echo; echo "PAGE=$(printf '%q' "$PAGE")"; } > "$STAGE/job.env"
if [ -n "$AUDIO" ]; then
  mkdir -p "$STAGE/audio"; ln -s "$(cd "$(dirname "$AUDIO")" && pwd)/$(basename "$AUDIO")" "$STAGE/audio/$(basename "$AUDIO")"
  echo "AUDIO=$(printf '%q' "audio/$(basename "$AUDIO")")" >> "$STAGE/job.env"
fi
B="$RUN/bundle.tar"
TARFLAGS=(); case "$(tar --version 2>/dev/null || true)" in *bsdtar*) TARFLAGS=(--no-mac-metadata --no-xattrs);; esac
COPYFILE_DISABLE=1 tar ${TARFLAGS[@]+"${TARFLAGS[@]}"} -chf "$B" --exclude='.git' --exclude='__pycache__' \
  --exclude='.DS_Store' --exclude='.renderfarm' -C "$RUN/stage" farm
rm -rf "$RUN/stage"
echo "bundle: $(du -h "$B" | cut -f1) ($(tar -tf "$B" | wc -l | tr -d ' ') entries), sha256 $(sha "$B" | cut -c1-16)..."
echo "job: $PAGE from $SITE${AUDIO:+, soundtrack $(basename "$AUDIO")}; candidates, in order: $CANDS"
echo "cost control: decision wait $DECIDE min${BUDGET:+, budget \$$BUDGET (auto decision)}, cap ${MAXS}s; upload=$UPLOAD"
if [ -n "$BUNDLE_OUT" ]; then cp "$B" "$BUNDLE_OUT"; echo "bundle copied to $BUNDLE_OUT"; fi
if [ "$GO" != 1 ]; then echo "dry run: add --go to create a pod (it bills from then on). Run folder: $RUN"; rm -f "$B"; exit 0; fi

# ---- go: the first candidate RunPod can give, its CPU checked, the bundle in over SSH, the job, the results out ---------
command -v runpodctl >/dev/null || { echo "runpodctl not found (docs/runpod.md)"; exit 1; }
[ -f "$KEY" ] && [ -f "$KEY.pub" ] || { echo "no SSH key at $KEY (and $KEY.pub): --ssh-key, or create runpodctl's (docs/runpod.md)"; exit 1; }
O=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20 -o ServerAliveInterval=30)
make_env() {  # $1 = price: the pod's environment (secrets only as RunPod references: their values never pass here)
  python3 - "$1" "$KEY.pub" "$BUDGET" "$MAXS" "$DECIDE" "$KEEP" "$UPLOAD" <<'EOF'
import json, sys
price, pub, budget, maxs, decide, keep, up = sys.argv[1:]
env = {"NVIDIA_DRIVER_CAPABILITIES": "all", "PUBLIC_KEY": open(pub).read().strip(),  # SSH starts only with a key to let in
       "RF_PRICE": price, "RF_BUDGET": budget, "RF_MAX_SECONDS": maxs, "RF_DECIDE_MINUTES": decide, "RF_KEEP_POD": keep,
       "RF_YT_UPLOAD": up}
if up == "1":  # RunPod fills these in from the account's secrets
    for name in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        env[name] = "{{ RUNPOD_SECRET_" + name.lower() + " }}"
print(json.dumps(env))
EOF
}
ID=""; FOLLOW=""; PODNAME=""; STARTED=0
pod_state() {  # gone, stopped or up: runpodctl's view of the pod
  local o; o=$(runpodctl pod get "$ID" 2>&1 || true)
  case "$o" in *'"not_found"'*) echo gone;; *EXITED*|*'"exited"'*|*'"stopped"'*) echo stopped;; *) echo up;; esac
}
drop() {  # delete the pod and make sure it is gone (a delete call can fail); 1 if it may still exist
  local i
  for i in $(seq 36); do
    runpodctl pod delete "$ID" >/dev/null 2>&1 || true
    sleep 5
    if [ "$(pod_state)" = gone ]; then echo "pod $ID deleted (confirmed)"; return 0; fi
  done
  echo "WARNING: pod $ID may still exist: check runpodctl pod list"; return 1
}
orphans() {  # a deploy request that failed or timed out may still have made a pod: find it by its unique name
  [ -n "$PODNAME" ] || return 0
  python3 "$HERE/deploy.py" --lookup "$PODNAME" 2>/dev/null || true
}
cleanup() {  # any exit before the job has started (an error under set -e, Ctrl+C, a lost connection): no pod is left
  if [ -n "$FOLLOW" ]; then kill "$FOLLOW" 2>/dev/null || true; fi
  if [ "$STARTED" = 1 ]; then return; fi
  if [ -z "$ID" ]; then for X in $(orphans); do ID=$X; done; fi
  if [ -n "$ID" ]; then echo "stopping before the job started: deleting pod $ID"; drop || true; fi
  rm -f "$B"; rm -rf "$RUN/parts"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
bail() { echo "$1"; exit 1; }
IFS=';' read -ra CL <<< "$CANDS"
n=0
for c in "${CL[@]}"; do
  n=$((n + 1)); IFS='|' read -r GPU CLOUD MINV PRICE <<< "$c"
  PODNAME="$NAME-$(date +%m%d-%H%M%S)-$n"
  if ! OUT=$(python3 "$HERE/deploy.py" --gpu "$GPU" --cloud "$CLOUD" --min-vcpu "$MINV" --min-mem 30 \
      --container-gb "$CONTAINER" --volume-gb "$VOLUME" --name "$PODNAME" --env "$(make_env "$PRICE")" 2>&1); then
    echo "$GPU $CLOUD $MINV+: not available ($(echo "$OUT" | tail -1 | cut -c1-160))"
    for X in $(orphans); do ID=$X; echo "a pod named $PODNAME exists anyway: deleting it"; drop || bail "stopping here"; done
    ID=""; continue
  fi
  echo "$OUT" | head -1; ID=$(echo "$OUT" | tail -1); echo "$ID" > "$RUN/pod-id"; echo "$GPU|$CLOUD|$PRICE" > "$RUN/gpu.txt"
  runpodctl pod logs "$ID" --follow > "$RUN/pod.log" 2>&1 & FOLLOW=$!
  INFO=""
  for i in $(seq 60); do INFO=$(runpodctl ssh info "$ID" 2>&1 || true); case "$INFO" in *root@*) break;; esac; sleep 5; done
  HOST=$(printf '%s' "$INFO" | grep -oE 'root@[0-9.]+' | head -1 | cut -d@ -f2 || true)
  PORT=$(printf '%s' "$INFO" | grep -oE '\-p [0-9]+' | head -1 | cut -d' ' -f2 || true)
  [ -n "$HOST" ] && [ -n "$PORT" ] || bail "no SSH address for pod $ID"
  SSH=(ssh "${O[@]}" -p "$PORT" "root@$HOST")
  # (no waiting on "Pod is ready" in the log: runpodctl's stream lags minutes; SSH answering is the test)
  for i in $(seq 12); do "${SSH[@]}" true 2>/dev/null && break; [ "$i" = 12 ] && bail "SSH never answered"; sleep 10; done
  CORES=$("${SSH[@]}" 'if [ -r /sys/fs/cgroup/cpu.max ]; then read -r q p < /sys/fs/cgroup/cpu.max;
    else q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us); fi
    case "$q" in max|-1) nproc;; *) awk -v q="$q" -v p="$p" "BEGIN { printf \"%.1f\", q / p }";; esac' 2>/dev/null || echo 0)
  # the quota can be below the listing (16 listed, 13.6 given): three quarters of it still renders well
  echo "CPU quota on this pod: $CORES cores (at least $(awk -v m="$MINV" 'BEGIN { print 0.75 * m }') wanted)"
  awk -v c="$CORES" -v m="$MINV" 'BEGIN { exit !(c + 0 >= 0.75 * m) }' && break
  echo "too little CPU: deleting pod $ID and trying the next candidate"; kill "$FOLLOW" 2>/dev/null || true; FOLLOW=""
  drop || bail "stopping here: make sure pod $ID is gone (runpodctl pod list)"
  ID=""
done
[ -n "$ID" ] || { echo "no candidate was available: nothing rendered, nothing spent"; exit 1; }
printf '%q ' "${SSH[@]}" > "$RUN/ssh.txt"; echo >> "$RUN/ssh.txt" # quoted, so a key path with spaces survives
echo "SSH: root@$HOST -p $PORT (saved in $RUN/ssh.txt)"

# the bundle in 8 MB pieces, each retried until its checksum matches on the pod
PARTS="$RUN/parts"; mkdir -p "$PARTS"; ( cd "$PARTS" && split -b 8m "$B" part. ) || bail "could not split the bundle"
"${SSH[@]}" 'mkdir -p /workspace/parts' || bail "no folder on the pod"
NP=$(ls "$PARTS" | wc -l | tr -d ' '); k=0
for f in "$PARTS"/part.*; do
  k=$((k + 1)); want=$(sha "$f"); ok=0
  for i in $(seq 8); do
    if scp -q "${O[@]}" -P "$PORT" "$f" "root@$HOST:/workspace/parts/" 2>/dev/null \
      && [ "$("${SSH[@]}" "sha256sum /workspace/parts/$(basename "$f")" 2>/dev/null | cut -d' ' -f1)" = "$want" ]; then ok=1; break; fi
    echo "  piece $k of $NP: try $i failed, retrying"; sleep 5
  done
  [ $ok = 1 ] || bail "piece $k of $NP would not copy"
done
echo "  $NP pieces copied"
REMOTE=$("${SSH[@]}" "cat /workspace/parts/part.* > /workspace/bundle.tar && rm -rf /workspace/parts && sha256sum /workspace/bundle.tar" \
  | cut -d' ' -f1) || bail "joining the pieces on the pod failed"
[ "$REMOTE" = "$(sha "$B")" ] || bail "the joined bundle doesn't match"
rm -rf "$PARTS"; echo "bundle copied and verified"
"${SSH[@]}" 'cd /workspace && rm -rf farm && tar --no-same-owner -xf bundle.tar && rm bundle.tar && (setsid nohup bash farm/cloud/runpod/job.sh > /proc/1/fd/1 2>&1 < /dev/null &) && echo "job started"' \
  || bail "the job did not start"
STARTED=1; rm -f "$B"
echo "The pod runs by itself now (Ctrl+C here only stops following it). Live view: python3 cloud/runpod/logserver.py $RUN"
[ -n "$BUDGET" ] || echo "After the scaling test it waits $DECIDE min for: cloud/runpod/pod.sh $RUN decide 'go <workers>'  (or 'stop')"

# the results: when the pod says they are ready, copy them, then tell it (polled over SSH, not read from the log: the
# log stream lags minutes; one tar stream, retried, is faster than scp -r). Gives up once the pod is gone or stopped.
deadline=$(( $(date +%s) + MAXS + 4 * 3600 ))
fails=0
until "${SSH[@]}" 'test -e /workspace/RESULTS_READY' 2>/dev/null; do
  fails=$((fails + 1)); sleep 15
  if [ $((fails % 20)) = 0 ]; then
    case "$(pod_state)" in
      gone) echo "the pod is gone before its results were ready: see $RUN/pod.log"; exit 1;;
      stopped) echo "the pod stopped before its results were ready; its volume keeps the work (docs/runpod.md)"; exit 1;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || { echo "no results yet, long past the cap: cloud/runpod/pod.sh $RUN log"; exit 1; }
  fi
done
collected=0
for i in $(seq 5); do
  if "${SSH[@]}" 'tar -C /workspace -cf - deliver' | tar -xf - -C "$RUN" && "${SSH[@]}" 'touch /workspace/COLLECTED'; then
    echo "results collected in $RUN/deliver"; collected=1; break
  fi
  echo "collecting the results: try $i failed"; sleep 10
done
if [ "$collected" != 1 ]; then
  echo "THE RESULTS WERE NOT COLLECTED. The pod waits for them, then stops instead of deleting itself if they hold the"
  echo "master: try again with cloud/runpod/pod.sh $RUN collect (start the pod first if it stopped), then pod.sh $RUN delete"
  exit 1
fi
grep -hE "PROJECTION|DECISION|render done|FRAMES|SYNC|LOUDNESS|youtu.be|STOPPING|FAILED|ALL DONE" "$RUN/deliver/logs/job.log" 2>/dev/null | tail -20 || true
