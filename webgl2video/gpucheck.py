"""python -m webgl2video gpucheck: does headless Chromium get the GPU for WebGL here, or a software renderer?

Launches Chromium with each GPU launch profile the driver uses (on Linux: ANGLE on Vulkan, then on EGL; elsewhere the
plain GPU flags), creates a WebGL2 context on a blank page, and prints the renderer it got. Exits 1 when no profile
gets hardware WebGL (SwiftShader, llvmpipe, lavapipe), so a container or a CI step fails loudly instead of rendering
on the CPU. docs/headless-gpu.md has the recipe that makes it pass on a Linux server with an NVIDIA GPU.
"""

import argparse
import json
import sys

from .browser import CPU_PROFILE, GPU_PROFILES, is_software

PROBE = """() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl2');
  if (!gl) return { ok: false, renderer: 'no WebGL2 context' };
  const dbg = gl.getExtension('WEBGL_debug_renderer_info');
  return { ok: true, renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
           vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
           maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE), floatTargets: !!gl.getExtension('EXT_color_buffer_float') };
}"""


def main(argv: list):
    ap = argparse.ArgumentParser(prog="python -m webgl2video gpucheck", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="one JSON line per profile instead of text")
    ap.add_argument("--all", action="store_true", help="try every profile, also after one has the GPU")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright
    hardware = None
    with sync_playwright() as pw:
        for prof in GPU_PROFILES + ([CPU_PROFILE] if args.all else []):
            kw = {"args": prof["args"], "headless": prof.get("headless", True)}
            if prof.get("channel"):
                kw["channel"] = prof["channel"]
            try:
                browser = pw.chromium.launch(**kw)
                try:
                    r = browser.new_page().evaluate(PROBE)
                finally:
                    browser.close()
            except Exception as e:
                r = {"ok": False, "renderer": f"did not start: {str(e).splitlines()[0][:200]}"}
            soft = not r["ok"] or is_software(r["renderer"])
            r.update(profile=prof["name"], hardware=not soft)
            print(json.dumps(r) if args.json else
                  f"{'GPU ' if not soft else 'NO  '} {prof['name']}: {r['renderer']}"
                  + (f" (textures up to {r['maxTexture']}, float targets {'yes' if r['floatTargets'] else 'no'})" if r["ok"] else ""),
                  flush=True)
            if not soft and hardware is None:
                hardware = prof["name"]
                if not args.all:
                    break
    if hardware:
        print(f"hardware WebGL: yes ({hardware})" if not args.json else "", end="\n" if not args.json else "")
        return
    sys.exit("hardware WebGL: NO. Chromium renders WebGL on the CPU here (a software renderer): see docs/headless-gpu.md")
