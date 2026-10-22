#!/bin/bash
# Talk to a render's pod from your machine, by its run folder (the one launch.sh printed).
#
#   cloud/runpod/pod.sh <run> decide "go 11"    the decision the job waits for after its scaling test ("stop" stops it)
#   cloud/runpod/pod.sh <run> add-worker 12     one more worker on the job's queue, once it has decided to go
#   cloud/runpod/pod.sh <run> log [lines]       the end of the job's log
#   cloud/runpod/pod.sh <run> collect           the results (and the master, when it was not uploaded) into <run>/deliver
#   cloud/runpod/pod.sh <run> resume            rerun the job on a pod you started again (it resumes where it stopped)
#   cloud/runpod/pod.sh <run> ssh               a shell on the pod
#   cloud/runpod/pod.sh <run> delete            delete the pod (and its volume), and confirm it is gone
#
# A pod that was stopped and started again has a new SSH port: the commands look it up (runpodctl ssh info) when the
# saved address does not answer.
set -euo pipefail
RUN=${1:?usage: pod.sh <run folder> decide|add-worker|log|collect|resume|ssh|delete ...}
CMD=${2:?decide, add-worker, log, collect, resume, ssh or delete}
[ -f "$RUN/pod-id" ] || { echo "$RUN has no pod-id: not a run folder launch.sh made"; exit 2; }
ID=$(cat "$RUN/pod-id")
SSH=()
if [ -s "$RUN/ssh.txt" ]; then eval "SSH=($(cat "$RUN/ssh.txt"))"; fi # written with printf %q by launch.sh

need_ssh() {  # a working SSH command in SSH, refreshed from runpodctl when the saved one does not answer
  if [ ${#SSH[@]} -gt 0 ] && "${SSH[@]}" true 2>/dev/null; then return 0; fi
  local info host port n
  info=$(runpodctl ssh info "$ID" 2>&1 || true)
  host=$(printf '%s' "$info" | grep -oE 'root@[0-9.]+' | head -1 | cut -d@ -f2 || true)
  port=$(printf '%s' "$info" | grep -oE '\-p [0-9]+' | head -1 | cut -d' ' -f2 || true)
  [ -n "$host" ] && [ -n "$port" ] || { echo "pod $ID has no SSH address now (is it running? runpodctl pod list)"; exit 1; }
  [ ${#SSH[@]} -gt 0 ] || { echo "$RUN/ssh.txt is missing"; exit 1; }
  n=${#SSH[@]}
  SSH=("${SSH[@]:0:$((n - 3))}" -p "$port" "root@$host") # the saved line ends in: -p <port> root@<host>
  "${SSH[@]}" true 2>/dev/null || { echo "pod $ID does not answer SSH at $host:$port"; exit 1; }
  printf '%q ' "${SSH[@]}" > "$RUN/ssh.txt"; echo >> "$RUN/ssh.txt"
}

case "$CMD" in
  decide)
    D=${3:?decide "go <workers>" or decide stop}
    [[ "$D" =~ ^(go\ [0-9]+|stop.*)$ ]] || { echo "decide 'go <workers>' or 'stop'"; exit 2; }
    need_ssh; "${SSH[@]}" "printf '%s\n' $(printf '%q' "$D") > /workspace/DECISION" && echo "sent: $D";;
  add-worker)
    K=${3:?add-worker <number>}; [[ "$K" =~ ^[0-9]+$ ]] || { echo "add-worker <number>"; exit 2; }
    need_ssh
    "${SSH[@]}" "(setsid nohup bash /workspace/farm/cloud/runpod/job.sh add-worker $K > /proc/1/fd/1 2>&1 < /dev/null &) && echo 'worker $K started'";;
  log) need_ssh; "${SSH[@]}" "tail -n ${3:-60} /workspace/logs/job.log";;
  collect)
    need_ssh
    "${SSH[@]}" 'test -d /workspace/deliver' || { echo "no results on the pod yet"; exit 1; }
    "${SSH[@]}" 'tar -C /workspace -cf - deliver' | tar -xf - -C "$RUN"
    "${SSH[@]}" 'touch /workspace/COLLECTED'
    echo "results collected in $RUN/deliver (delete the pod when you are done with it: pod.sh $RUN delete)";;
  resume)
    need_ssh
    "${SSH[@]}" 'cd /workspace && (setsid nohup bash farm/cloud/runpod/job.sh > /proc/1/fd/1 2>&1 < /dev/null &) && echo "job restarted"';;
  ssh) need_ssh; exec "${SSH[@]}";;
  delete)
    for i in $(seq 36); do
      runpodctl pod delete "$ID" >/dev/null 2>&1 || true
      sleep 5
      case "$(runpodctl pod get "$ID" 2>&1 || true)" in *'"not_found"'*) echo "pod $ID deleted (confirmed)"; exit 0;; esac
    done
    echo "WARNING: pod $ID may still exist: check runpodctl pod list"; exit 1;;
  *) echo "unknown command $CMD"; exit 2;;
esac
