"""Log every request path then forward to the real gateway."""
from __future__ import annotations

import http.server
import socketserver
import urllib.error
import urllib.request

UPSTREAM = "http://127.0.0.1:8787"
PORT = 8799
LOG = "data/oc_probe.log"


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        line = f"{self.command} {self.path}\n"
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
            for k, v in self.headers.items():
                if k.lower() in ("host", "authorization", "x-api-key", "anthropic-version", "content-type"):
                    f.write(f"  {k}: {v}\n")
            f.write("\n")
        url = UPSTREAM + self.path
        req = urllib.request.Request(url, data=body if body else None, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "connection", "content-length"):
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("content-type", e.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = str(e).encode()
            self.send_response(502)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        self._relay()

    def do_POST(self):
        self._relay()

    def do_PUT(self):
        self._relay()

    def do_DELETE(self):
        self._relay()

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    Server(("127.0.0.1", PORT), H).serve_forever()
