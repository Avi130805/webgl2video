#!/usr/bin/env python3
"""Create a RunPod GPU pod through the GraphQL API, which can demand what runpodctl cannot: a minimum number of CPU
cores (the render is CPU-bound, and the cores a GPU comes with vary by host) and a public IP (SSH on Community pods).
Prints the new pod's id on the last line, or exits non-zero with RunPod's answer.

The API key comes from RUNPOD_API_KEY, else from runpodctl's config (~/.runpod/config.toml). It is never printed.

  deploy.py --gpu "NVIDIA RTX 4000 Ada Generation" --cloud SECURE --min-vcpu 16 --name render --env '{"K": "V"}'
            [--min-mem 30] [--container-gb 30] [--volume-gb 40] [--ports 22/tcp] [--image runpod/base:1.0.2-ubuntu2404]
            [--dc <datacenter id>] [--dry-run]
  deploy.py --lookup <name>      the ids of your pods with that exact name, one a line (launch.sh deletes a pod that a
                                 failed or timed-out request made anyway)
"""
import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--gpu", help="a RunPod GPU type id, e.g. 'NVIDIA RTX 4000 Ada Generation'")
ap.add_argument("--lookup", metavar="NAME", help="print the ids of your pods with this name, and stop")
ap.add_argument("--cloud", default="SECURE", choices=("SECURE", "COMMUNITY"))
ap.add_argument("--min-vcpu", type=int, default=8)
ap.add_argument("--min-mem", type=int, default=24)
ap.add_argument("--container-gb", type=int, default=20)
ap.add_argument("--volume-gb", type=int, default=0, help="a pod volume at /workspace (kept when the pod is stopped)")
ap.add_argument("--ports", default="22/tcp")
ap.add_argument("--image", default="runpod/base:1.0.2-ubuntu2404")
ap.add_argument("--name", default="render")
ap.add_argument("--env", default="{}", help="a JSON object of environment variables")
ap.add_argument("--dc", default="", help="a datacenter id (optional)")
ap.add_argument("--dry-run", action="store_true", help="print the request (without the key) and stop")
A = ap.parse_args()
if not (A.gpu or A.lookup):
    ap.error("--gpu (or --lookup)")

q = lambda s: json.dumps(s)  # GraphQL string literals are JSON strings
env = ", ".join(f"{{ key: {q(k)}, value: {q(str(v))} }}" for k, v in json.loads(A.env).items())
inp = (f"cloudType: {A.cloud}, gpuCount: 1, gpuTypeId: {q(A.gpu)}, minVcpuCount: {A.min_vcpu}, minMemoryInGb: {A.min_mem}, "
       f"containerDiskInGb: {A.container_gb}, volumeInGb: {A.volume_gb}, ports: {q(A.ports)}, imageName: {q(A.image)}, "
       f"name: {q(A.name)}, dockerArgs: \"\", supportPublicIp: true, env: [{env}]"
       + (", volumeMountPath: \"/workspace\"" if A.volume_gb else "") + (f", dataCenterId: {q(A.dc)}" if A.dc else ""))
query = f"mutation {{ podFindAndDeployOnDemand(input: {{ {inp} }}) {{ id costPerHr vcpuCount memoryInGb machine {{ gpuDisplayName }} }} }}"
if A.dry_run:
    shown = re.sub(r'(value: )"[^"]*"', r'\1"..."', query)  # env values can hold secret references: not shown
    print(shown)
    sys.exit(0)

key = os.environ.get("RUNPOD_API_KEY", "")
if not key:
    cfg = pathlib.Path.home() / ".runpod" / "config.toml"
    m = re.search(r"(?im)^\s*api_?key\s*=\s*[\"']([^\"']+)[\"']", cfg.read_text()) if cfg.exists() else None
    if not m:
        sys.exit("no RunPod API key: set RUNPOD_API_KEY, or run `runpodctl config --apiKey ...` once")
    key = m.group(1)


def graphql(text: str) -> dict:
    req = urllib.request.Request("https://api.runpod.io/graphql", data=json.dumps({"query": text}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        sys.exit(f"RunPod refused ({e.code}): {e.read().decode()[:400]}")
    except (urllib.error.URLError, OSError) as e:  # a timeout: the pod may exist anyway (launch.sh looks it up)
        sys.exit(f"no answer from RunPod ({e})")


if A.lookup:
    d = graphql("query { myself { pods { id name } } }")
    pods = ((d.get("data") or {}).get("myself") or {}).get("pods")
    if pods is None:
        sys.exit(f"could not list pods: {json.dumps(d.get('errors') or d)[:400]}")
    for pod in pods:
        if pod.get("name") == A.lookup:
            print(pod["id"])
    sys.exit(0)
d = graphql(query)
if d.get("errors") or not (d.get("data") or {}).get("podFindAndDeployOnDemand"):
    sys.exit(f"no pod: {json.dumps(d.get('errors') or d)[:400]}")
p = d["data"]["podFindAndDeployOnDemand"]
print(f"pod {p['id']}: {(p.get('machine') or {}).get('gpuDisplayName')}, {p.get('vcpuCount')} vCPU,"
      f" {p.get('memoryInGb')} GB RAM, ${p.get('costPerHr')}/h")
print(p["id"])
