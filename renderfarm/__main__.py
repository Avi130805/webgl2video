"""python -m renderfarm <command> ...

  render    render a page into a video (resumable segments, then the join)
  probe     which browser profile gets hardware WebGL here, and what the page says of itself
  exact     check the page's GPU conversion (NV12, P010) against a float64 reference on this GPU
  qa        check a rendered range: unexpected cuts, single-frame flashes, a sheet of stills
  verify    check a finished video: frames, loudness, sync with the soundtrack, stills
  upload    upload a video to YouTube (Private unless told otherwise)

`python -m renderfarm <command> --help` for each.
"""

import sys


def main():
    cmds = {
        "render": lambda a: __import__("renderfarm.render", fromlist=["main"]).main(a),
        "probe": lambda a: __import__("renderfarm.render", fromlist=["probe"]).probe(a),
        "exact": lambda a: __import__("renderfarm.exact", fromlist=["main"]).main(a),
        "qa": lambda a: __import__("renderfarm.qa", fromlist=["main"]).main(a),
        "verify": lambda a: __import__("renderfarm.verify", fromlist=["main"]).main(a),
        "upload": lambda a: __import__("renderfarm.youtube", fromlist=["main"]).main(a),
    }
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    cmds[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
