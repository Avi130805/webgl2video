#!/usr/bin/env python3
"""A live view of a cloud render in the browser (runs on your machine): http://localhost:8787

Streams, as they happen, over the SSH line launch.sh saved in <run>/ssh.txt:
  - the job's own log (cloud/runpod/job.sh: phases, the scaling test, the decision, jobs and their checks, progress);
  - the workers' logs (render progress, the driver's STATS timing every 250 frames, each job's check);
  - the overall speed: frames rendered over the last two minutes across every worker (rate.py, every 20 s).

  python3 cloud/runpod/logserver.py [<run folder>] [--port 8787] [--workspace /workspace]
      (default: the newest run under ~/.renderfarm/runs)
"""
import collections
import glob
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8787
WS = sys.argv[sys.argv.index("--workspace") + 1] if "--workspace" in sys.argv else "/workspace"
args = [a for a in args if a not in (str(PORT), WS)]
if args and not args[0].isdigit():
    RUN = pathlib.Path(args[0]).expanduser()
else:
    runs = [pathlib.Path(p) for p in glob.glob(os.path.expanduser("~/.renderfarm/runs/*")) if os.path.exists(os.path.join(p, "ssh.txt"))]
    if not runs:
        sys.exit("no run folder with an ssh.txt: give the one launch.sh printed")
    RUN = max(runs, key=lambda p: p.stat().st_mtime)
HERE = pathlib.Path(__file__).resolve().parent

lines = collections.deque(maxlen=20000)  # (seq, source, text)
seq = 0
cond = threading.Condition()
status = {"rate": "", "progress": "", "phase": "", "total": 0, "done": False}


def add(source, text):
    global seq
    with cond:
        seq += 1
        lines.append((seq, source, text))
        cond.notify_all()


def ssh_cmd():
    return shlex.split((RUN / "ssh.txt").read_text().strip())


FOLLOW = r'''
import glob, os, time
pos, first = {}, True
pats = ['job.log', 'job-*.log', 'check-*.log', 'join.log', 'verify.log', 'upload.log', 'exact.log', 'probe.log']
while True:
    files = sorted(f for p in pats for f in glob.glob('/workspace/logs/' + p))
    for f in files:
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        p = pos.get(f)
        if p is None:
            p = max(0, size - (20000 if f.endswith('/job.log') else 3000)) if first else 0
        if size > p:
            with open(f, 'rb') as fh:
                fh.seek(p)
                data = fh.read(size - p)
            name = os.path.basename(f)[:-4]
            for line in data.decode('utf-8', 'replace').replace('\r', '\n').splitlines():
                if line.strip():
                    print(name + '\t' + line, flush=True)
        pos[f] = max(size, p)
    first = False
    time.sleep(1)
'''


def follow():
    """Every log on the pod, followed over SSH (reconnects; stops once the job has ended or the pod is gone)."""
    fails = 0
    while not status["done"] and fails < 30:
        try:
            pr = subprocess.Popen(ssh_cmd() + ["python3 -u -c " + shlex.quote(FOLLOW.replace("/workspace", WS))], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, errors="replace")
            for line in pr.stdout:
                name, _, text = line.rstrip("\n").partition("\t")
                if name == "job":
                    add("job", text)
                    m = re.search(r"(PHASE [^\n]*|STAGE s\d+-w\d+: [^;]*;[^;]*|DECISION: [^\n]*|PROGRESS [^\n]*)", text)
                    if m:
                        status["progress" if m.group(1).startswith("PROGRESS") else "phase"] = m.group(1)[:160]
                    m = re.search(r"PROGRESS \d+ of (\d+)|\"frames\": (\d+)", text)
                    if m:
                        status["total"] = int(m.group(1) or m.group(2))
                    if "ALL DONE" in text or "STOPPING (work kept)" in text:
                        status["done"] = True
                else:
                    add("worker", f"[{name}] {text}")
                fails = 0
            pr.wait()
        except OSError:
            pass
        fails += 1
        time.sleep(5)
    add("job", "(the job has ended or the pod is gone: following stopped)")


def poll_rate():
    """The overall speed, every 20 s: rate.py over SSH."""
    rate_py = (HERE / "rate.py").read_text()
    while not status["done"]:
        try:
            out = subprocess.run(ssh_cmd() + [f"python3 - {shlex.quote(WS)}"], input=rate_py, capture_output=True, text=True,
                                 timeout=60).stdout.strip()
            if out:
                status["rate"] = out
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(20)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Render: live</title>
<style>
:root{--bg:#0d0f12;--panel:#14171c;--line:#232830;--text:#d7dce2;--dim:#7d8693;--accent:#e6b450;--ok:#7fd88f;--bad:#ff6b6b;--info:#73b8ff}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--panel:#fff;--line:#dde1e6;--text:#1d2329;--dim:#6b7380;--accent:#9a6700;--ok:#1a7f37;--bad:#cf222e;--info:#0969da}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;z-index:2}
h1{font:600 15px system-ui,sans-serif;margin:0 0 6px}
.stats{display:flex;flex-wrap:wrap;gap:6px 18px}.stats b{color:var(--accent);font-weight:600}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent);width:0}
.tools{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap}
.tools input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:4px;padding:4px 8px;font:inherit;min-width:220px}
.tools label{color:var(--dim)}
main{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px 16px}
@media (max-width:900px){main{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:6px;display:flex;flex-direction:column;height:calc(100vh - 190px);min-height:300px}
section h2{font:600 12px system-ui,sans-serif;color:var(--dim);margin:0;padding:8px 10px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.04em}
.log{overflow:auto;padding:6px 10px;flex:1;white-space:pre-wrap;word-break:break-word}
.l{padding:1px 0}.l.stage,.l.dec{color:var(--accent)}.l.bad{color:var(--bad)}.l.ok{color:var(--ok)}.l.stats{color:var(--dim)}.l.info{color:var(--info)}
</style></head><body>
<header><h1>Render: live</h1>
<div class="stats"><span>speed <b id="rate">...</b></span><span id="prog"></span><span>phase <b id="phase">...</b></span></div>
<div class="bar"><i id="bar"></i></div>
<div class="tools"><input id="q" placeholder="filter (e.g. JOB, STATS, 21500)"><label><input type="checkbox" id="follow" checked> follow</label></div>
</header>
<main><section><h2>Job (phases, scaling test, decision, jobs, checks)</h2><div class="log" id="job"></div></section>
<section><h2>Workers (progress and timing)</h2><div class="log" id="worker"></div></section></main>
<script>
const q = document.getElementById('q'), follow = document.getElementById('follow');
function cls(t){ if(/FAILED|STOPPING|Traceback|rror/.test(t)) return 'bad';
  if(/STAGE|PROJECTION|AUTO/.test(t)) return 'stage'; if(/DECISION/.test(t)) return 'dec';
  if(/JOB .* done|EXACT|youtu\.be|ALL DONE|FRAMES/.test(t)) return 'ok'; if(/STATS/.test(t)) return 'stats';
  if(/PHASE|PROGRESS/.test(t)) return 'info'; return ''; }
function show(el){ const f = q.value.toLowerCase(); el.style.display = !f || el.textContent.toLowerCase().includes(f) ? '' : 'none'; }
q.addEventListener('input', () => document.querySelectorAll('.l').forEach(show));
const es = new EventSource('/events');
es.onmessage = (e) => { const d = JSON.parse(e.data), box = document.getElementById(d.s === 'job' ? 'job' : 'worker');
  const el = document.createElement('div'); el.className = 'l ' + cls(d.t); el.textContent = d.t; show(el); box.appendChild(el);
  while (box.childElementCount > 4000) box.firstChild.remove();
  if (follow.checked) box.scrollTop = box.scrollHeight; };
async function tick(){ try { const s = await (await fetch('/status')).json();
  document.getElementById('rate').textContent = s.rate || '...'; document.getElementById('phase').textContent = s.phase || '...';
  const m = /(\d+) frames rendered/.exec(s.rate || ''), p = /PROGRESS (\d+) of (\d+)/.exec(s.progress || '');
  const done = p ? +p[1] : (m ? +m[1] : 0);
  document.getElementById('prog').textContent = s.progress || '';
  if (s.total) document.getElementById('bar').style.width = (100 * done / s.total).toFixed(1) + '%'; } catch(e){} }
tick(); setInterval(tick, 5000);
</script></body></html>'''


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            body = json.dumps(status).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = 0
            try:
                while True:
                    with cond:
                        new = [x for x in lines if x[0] > last]
                        if not new:
                            cond.wait(15)
                            new = [x for x in lines if x[0] > last]
                    if not new:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    for s, src, text in new:
                        self.wfile.write(f"id: {s}\ndata: {json.dumps({'s': src, 't': text})}\n\n".encode())
                    self.wfile.flush()
                    last = new[-1][0]
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    if not (RUN / "ssh.txt").exists():
        sys.exit(f"{RUN} has no ssh.txt: the pod never answered SSH")
    for f in (follow, poll_rate):
        threading.Thread(target=f, daemon=True).start()
    print(f"Render, live: http://localhost:{PORT}  (run folder {RUN})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
