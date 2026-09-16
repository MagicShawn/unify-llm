"""The diagnostic relay must preserve requests without persisting credentials."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import oc_probe


@pytest.fixture()
def relay(tmp_path, monkeypatch):
    class EchoHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = json.dumps(
                {"path": self.path, "headers": dict(self.headers), "body": body.decode()}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    log = tmp_path / "probe.log"
    with HTTPServer(("127.0.0.1", 0), EchoHandler) as upstream:
        monkeypatch.setattr(oc_probe, "UPSTREAM", f"http://127.0.0.1:{upstream.server_port}")
        monkeypatch.setattr(oc_probe, "LOG", str(log))
        with oc_probe.Server(("127.0.0.1", 0), oc_probe.H) as proxy:
            threads = [
                threading.Thread(target=server.serve_forever, daemon=True)
                for server in (upstream, proxy)
            ]
            for thread in threads:
                thread.start()
            try:
                yield proxy.server_address, log
            finally:
                proxy.shutdown()
                upstream.shutdown()
                for thread in threads:
                    thread.join(timeout=5)


@pytest.mark.parametrize(
    ("headers", "query", "secret"),
    [
        ({"Authorization": "Bearer synthetic-bearer-secret"}, "", "synthetic-bearer-secret"),
        ({"x-api-key": "synthetic-api-secret"}, "", "synthetic-api-secret"),
        ({}, "?api_key=synthetic-query-secret", "synthetic-query-secret"),
    ],
)
def test_relay_does_not_log_credentials(relay, headers, query, secret):
    address, log = relay
    path = "/v1/messages" + query
    body = '{"model":"synthetic-model"}'
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.request("POST", path, body, headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        echoed = json.loads(response.read())
        assert response.status == 200
    finally:
        connection.close()

    assert echoed["path"] == path
    assert echoed["body"] == body
    received_headers = {key.lower(): value for key, value in echoed["headers"].items()}
    for key, value in headers.items():
        assert received_headers[key.lower()] == value

    logged = log.read_text(encoding="utf-8")
    assert "POST /v1/messages\n" in logged
    assert "application/json" in logged
    assert secret not in logged
