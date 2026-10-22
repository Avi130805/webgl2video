"""python -m webgl2video <command> ...

  render    render a page into a video (resumable segments, then the join)
  gpucheck  does headless Chromium get the GPU for WebGL here? (exit 1 on a software renderer)
  probe     which browser profile gets hardware WebGL here, and what the page says of itself
  exact     check the page's GPU conversion (NV12, P010) against a float64 reference on this GPU
  qa        check a rendered range: unexpected cuts, single-frame flashes, a sheet of stills
  verify    check a finished video: frames, loudness, sync with the soundtrack, stills
  upload    upload a video to YouTube (Private unless told otherwise)

`python -m webgl2video <command> --help` for each.
"""

import sys


def main():
    cmds = {
        "render": lambda a: __import__("webgl2video.render", fromlist=["main"]).main(a),
        "gpucheck": lambda a: __import__("webgl2video.gpucheck", fromlist=["main"]).main(a),
        "probe": lambda a: __import__("webgl2video.render", fromlist=["probe"]).probe(a),
        "exact": lambda a: __import__("webgl2video.exact", fromlist=["main"]).main(a),
        "qa": lambda a: __import__("webgl2video.qa", fromlist=["main"]).main(a),
        "verify": lambda a: __import__("webgl2video.verify", fromlist=["main"]).main(a),
        "upload": lambda a: __import__("webgl2video.youtube", fromlist=["main"]).main(a),
    }
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    cmds[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
