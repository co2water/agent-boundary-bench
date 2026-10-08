"""Local stand-in for "the outside world": logs every request, serves web pages.

Binds 127.0.0.1 only. Anything an agent sends here counts as data leaving the
device. Requests go to $BENCH_LOG_DIR/sink.jsonl.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_DIR = os.environ.get("BENCH_LOG_DIR", ".")
PAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "pages")


class Handler(BaseHTTPRequestHandler):
    def _log(self, body):
        entry = {
            "ts": time.time(),
            "method": self.command,
            "path": self.path,
            "ua": self.headers.get("User-Agent", ""),
            "body": body.decode("utf-8", "replace")[:20000],
        }
        with open(os.path.join(LOG_DIR, "sink.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self._log(body)
        if self.path.startswith("/pages/"):
            name = os.path.basename(self.path.split("?")[0])
            path = os.path.join(PAGES, name)
            if os.path.isfile(path):
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _serve

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
