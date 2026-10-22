"""python -m webgl2video upload: upload a video to YouTube, from any machine (a laptop, a cloud pod). Standard library
only; the upload is resumable (64 MB pieces, retried) and the video is Private unless told otherwise.

Credentials come from environment variables (on a RunPod pod: RunPod secrets), or from an env file of NAME=value lines.
Nothing here ever prints a secret: not the client id or secret, not the refresh token, not the access token.

  YT_CLIENT_ID, YT_CLIENT_SECRET   an OAuth client of your Google Cloud project (type "Desktop app"), with the
                                   YouTube Data API v3 enabled
  YT_REFRESH_TOKEN                 a refresh token for the channel, granted the youtube.upload scope (docs/youtube.md)
  --profile NAME                   reads YT_<NAME>_CLIENT_ID, YT_<NAME>_CLIENT_SECRET, YT_<NAME>_REFRESH_TOKEN instead
                                   (several channels or apps side by side)

  python -m webgl2video upload --check                       which channel the login opens (read-only)
  python -m webgl2video upload --file film.mp4 --title "..." [--description-file d.txt] [--tags "a, b"]
"""

import argparse
import http.client
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def request(method, url, data=None, headers=None, timeout=120):
    """(status, headers, body); status 0 for a connection that dropped or answered nonsense (worth a retry)."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:  # (YouTube's 308 "resume incomplete" arrives here too)
        with e:
            return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return 0, {}, str(e).encode()


RETRY = (0, 500, 502, 503, 504)


def retried(fn, tries=5):
    """fn() until its status is not a transient failure (backing off 1, 2, 4 ... s); the last answer."""
    for attempt in range(tries):
        st, hd, body = fn()
        if st not in RETRY:
            break
        time.sleep(2 ** attempt)
    return st, hd, body


def google_error(body: bytes) -> str:
    try:
        j = json.loads(body)
        e = j.get("error")
        return e.get("message", str(e)) if isinstance(e, dict) else f"{e}: {j.get('error_description', '')}"
    except Exception:
        return body[:200].decode("utf-8", "replace")


def load_env(env_file) -> dict:
    env = {}
    if env_file:
        p = pathlib.Path(env_file).expanduser()
        if not p.exists():
            sys.exit(f"--env-file {env_file}: no such file")
        for line in p.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("YT_")})  # the environment wins (a pod's secrets)
    return env


def main(argv: list):
    ap = argparse.ArgumentParser(prog="python -m webgl2video upload", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="", help="read YT_<PROFILE>_* variables")
    ap.add_argument("--env-file", help="a file of NAME=value lines (the environment wins)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--title")
    ap.add_argument("--description-file")
    ap.add_argument("--tags", default="")
    ap.add_argument("--category", default="28", help="28 = Science & Technology")
    ap.add_argument("--synthetic", action="store_true", help="declare altered or synthetic content (YouTube's disclosure)")
    ap.add_argument("--language", default="en", help="the title and description language, and the spoken language")
    ap.add_argument("--privacy", default="private", choices=("private", "unlisted", "public"))
    ap.add_argument("--chunk-mb", type=int, default=64)
    A = ap.parse_args(argv)

    env = load_env(A.env_file)
    pre = f"YT_{A.profile.upper()}_" if A.profile else "YT_"

    def need(name):
        if not env.get(pre + name):
            sys.exit(f"missing {pre + name} (not in the environment{' or ' + A.env_file if A.env_file else ''})")
        return env[pre + name]

    form = urllib.parse.urlencode({"client_id": need("CLIENT_ID"), "client_secret": need("CLIENT_SECRET"),
                                   "refresh_token": need("REFRESH_TOKEN"), "grant_type": "refresh_token"}).encode()

    def login():
        """A fresh access token (they last an hour; a slow upload outlives one)."""
        st, _, body = retried(lambda: request("POST", "https://oauth2.googleapis.com/token", form,
                                              {"Content-Type": "application/x-www-form-urlencoded"}))
        if st != 200:
            sys.exit(f"the login was refused ({st}): {google_error(body)}")
        tok = json.loads(body)
        return {"Authorization": f"Bearer {tok['access_token']}"}, tok

    auth, tok = login()
    print("login ok; allowed:", ", ".join(s.rsplit("/", 1)[-1] for s in tok.get("scope", "").split()) or "(not stated)")

    if A.check:
        st, _, body = request("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&mine=true", headers=auth)
        if st == 200 and json.loads(body).get("items"):
            c = json.loads(body)["items"][0]
            s = c.get("statistics", {})
            print(f"channel: {c['snippet']['title']} ({c['id']}), {s.get('subscriberCount', '?')} subscribers,"
                  f" {s.get('videoCount', '?')} videos")
        else:
            print(f"channel name not readable with these permissions ({st}): {google_error(body)}")
        return

    if not (A.file and A.title):
        sys.exit("--file and --title are needed to upload (or --check)")
    path = pathlib.Path(A.file).expanduser()
    done = path.with_name(path.name + ".youtube.txt")  # written after an upload: the same file is never uploaded twice
    if done.exists():
        print(f"already uploaded: {done.read_text().strip()}")
        return
    size = path.stat().st_size
    meta = {"snippet": {"title": A.title, "categoryId": A.category,
                        "description": pathlib.Path(A.description_file).read_text() if A.description_file else "",
                        "tags": [t.strip() for t in A.tags.split(",") if t.strip()],
                        "defaultLanguage": A.language, "defaultAudioLanguage": A.language},
            "status": {"privacyStatus": A.privacy, "selfDeclaredMadeForKids": False, "embeddable": True,
                       "license": "youtube", "publicStatsViewable": True,
                       **({"containsSyntheticMedia": True} if A.synthetic else {})}}
    ctype = {".mkv": "video/x-matroska", ".mov": "video/quicktime"}.get(path.suffix.lower(), "video/mp4")
    st, hd, body = retried(lambda: request(
        "POST", "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        json.dumps(meta).encode(), {**auth, "Content-Type": "application/json; charset=UTF-8",
                                    "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": ctype}))
    if st != 200:
        sys.exit(f"YouTube refused the upload ({st}): {google_error(body)}")
    loc = hd.get("Location") or hd.get("location")
    print(f"uploading {path.name} ({size / 1e9:.2f} GB) as {A.privacy} ...", flush=True)
    chunk, sent, t0 = A.chunk_mb * 1024 * 1024, 0, time.time()
    with path.open("rb") as f:
        while sent < size:
            f.seek(sent)
            data = f.read(chunk)
            end = sent + len(data) - 1
            for attempt in range(6):
                st, hd, body = request("PUT", loc, data, {**auth, "Content-Length": str(len(data)),
                                                          "Content-Range": f"bytes {sent}-{end}/{size}"}, timeout=600)
                if st in (200, 201, 308):
                    break
                if st == 401:  # the access token expired during a long upload: sign in again, send the piece again
                    auth = login()[0]
                    continue
                if st in RETRY:
                    time.sleep(2 ** attempt)
                    # where does the upload stand? (an empty PUT with "bytes */size" answers 308 with the Range held)
                    st, hd, body = request("PUT", loc, b"", {**auth, "Content-Length": "0", "Content-Range": f"bytes */{size}"})
                    if st in (200, 201):
                        break
                    if st == 308:
                        rng = hd.get("Range") or hd.get("range")
                        held = int(rng.split("-")[1]) + 1 if rng else 0
                        if held != sent:  # resume from what YouTube holds
                            break
                    continue
                sys.exit(f"upload failed at {sent / 1e9:.2f} GB ({st}): {google_error(body)}")
            else:
                sys.exit(f"upload failed at {sent / 1e9:.2f} GB after 6 tries ({st}): {google_error(body)}")
            if st in (200, 201):
                v = json.loads(body)
                print(f"done in {time.time() - t0:.0f} s: https://youtu.be/{v['id']} ({v['status']['privacyStatus']})")
                done.write_text(f"https://youtu.be/{v['id']}\n")
                return
            rng = hd.get("Range") or hd.get("range")  # 308: what YouTube has so far
            sent = int(rng.split("-")[1]) + 1 if rng else 0
            print(f"  {sent / 1e9:.2f} of {size / 1e9:.2f} GB ({sent / max(1, time.time() - t0) / 1e6:.1f} MB/s)", flush=True)
    sys.exit("upload ended without a video id")
