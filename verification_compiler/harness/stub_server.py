"""Null services for spec calibration. A discriminating acceptance test must fail against all of them.

  not_found     every request -> 404 {"detail": "Not Found"}
  server_error  every request -> 500 {"detail": "Internal Server Error"}
  empty_ok      every request -> 200 {}
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = {
    "not_found": (404, {"detail": "Not Found"}),
    "server_error": (500, {"detail": "Internal Server Error"}),
    "empty_ok": (200, {}),
}
STATUS, BODY = MODES[sys.argv[1]]
PAYLOAD = json.dumps(BODY).encode()


class Handler(BaseHTTPRequestHandler):
    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(PAYLOAD)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _respond

    def log_message(self, format, *args):  # noqa: A002 - signature of the base class
        pass


ThreadingHTTPServer(("0.0.0.0", int(sys.argv[2])), Handler).serve_forever()
