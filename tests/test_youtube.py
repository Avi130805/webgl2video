"""The uploader against a local stand-in for Google's endpoints: the resumable protocol (pieces, 308 and Range), a
server error mid-upload (it asks where the upload stands, then resumes), an access token that expires mid-upload (it
signs in again), the file arriving whole, and no secret in anything it prints."""

import contextlib
import http.server
import io
import json
import threading
import unittest
import urllib.parse

from common import tmpdir

import renderfarm.youtube as yt

SECRETS = ("client-secret-value", "refresh-token-value")


class FakeGoogle(http.server.BaseHTTPRequestHandler):
    logins, data, size, puts, failed_once = 0, bytearray(), 0, 0, False

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        cls = type(self)
        if self.path.startswith("/token"):
            form = urllib.parse.parse_qs(body.decode())
            assert form["refresh_token"] == ["refresh-token-value"], form
            cls.logins += 1
            tok = {"access_token": f"access-{cls.logins}", "scope": "https://www.googleapis.com/auth/youtube.upload"}
            return self.reply(200, json.dumps(tok).encode())
        assert json.loads(body)["status"]["privacyStatus"] == "private"
        cls.size = int(self.headers["X-Upload-Content-Length"])
        self.reply(200, b"{}", [("Location", f"http://127.0.0.1:{self.server.server_address[1]}/session")])

    def do_PUT(self):
        cls = type(self)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        rng = self.headers["Content-Range"]
        held = [("Range", f"bytes=0-{len(cls.data) - 1}")] if cls.data else []
        if rng.startswith("bytes */"):  # where does the upload stand?
            return self.reply(308, b"", held)
        if self.headers["Authorization"] == "Bearer access-1" and cls.puts >= 1:  # the first token expires
            return self.reply(401, b'{"error": {"message": "expired"}}')
        cls.puts += 1
        if cls.puts == 3 and not cls.failed_once:  # one server error, after the bytes may or may not have landed
            cls.failed_once = True
            return self.reply(503, b'{"error": {"message": "backend error"}}')
        start = int(rng.split()[1].split("-")[0])
        assert start == len(cls.data), (start, len(cls.data))
        cls.data += body
        if len(cls.data) == cls.size:
            return self.reply(200, json.dumps({"id": "abc123", "status": {"privacyStatus": "private"}}).encode())
        self.reply(308, b"", [("Range", f"bytes=0-{len(cls.data) - 1}")])


class Upload(unittest.TestCase):
    def test_resumable_upload(self):
        d = tmpdir(self)
        video = d / "film.mp4"
        payload = bytes(range(256)) * (4 * 4096 + 7)  # a little over 4 MB: five 1 MB pieces
        video.write_bytes(payload)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        real = yt.request

        def local(method, url, *a, **k):
            url = url.replace("https://oauth2.googleapis.com", base).replace("https://www.googleapis.com", base)
            return real(method, url, *a, **k)
        yt.request = local
        self.addCleanup(setattr, yt, "request", real)
        sleep = yt.time.sleep
        yt.time.sleep = lambda s: None
        self.addCleanup(setattr, yt.time, "sleep", sleep)
        env = d / "yt.env"
        env.write_text("YT_CLIENT_ID=client-id\nYT_CLIENT_SECRET=client-secret-value\nYT_REFRESH_TOKEN=refresh-token-value\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            yt.main(["--env-file", str(env), "--file", str(video), "--title", "a test", "--chunk-mb", "1"])
        text = out.getvalue()
        self.assertEqual(bytes(FakeGoogle.data), payload)
        self.assertIn("https://youtu.be/abc123 (private)", text)
        self.assertEqual((video.parent / "film.mp4.youtube.txt").read_text().strip(), "https://youtu.be/abc123")
        self.assertEqual(FakeGoogle.logins, 2)  # signed in again when the first token expired
        self.assertTrue(FakeGoogle.failed_once)
        for s in SECRETS + ("access-1", "access-2"):
            self.assertNotIn(s, text)
        with contextlib.redirect_stdout(io.StringIO()) as again:  # the same file is never uploaded twice
            yt.main(["--env-file", str(env), "--file", str(video), "--title", "a test"])
        self.assertIn("already uploaded", again.getvalue())


if __name__ == "__main__":
    unittest.main()
