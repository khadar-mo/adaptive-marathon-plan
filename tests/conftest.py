from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

import runs


@pytest.fixture
def cli(monkeypatch, tmp_path, capsys):
    # Exercise argument parsing, settings and token rotation without reading
    # the developer's .env, environment credentials or saved Strava grants.
    monkeypatch.setattr(runs, "__file__", str(tmp_path / "runs.py"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in tuple(runs.os.environ):
        if name.startswith(("STRAVA_", "GARMIN_")):
            monkeypatch.delenv(name)

    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["runs.py", *args])
        code = runs.main()
        output = capsys.readouterr()
        return code, output.out, output.err

    return invoke


@pytest.fixture
def upstream(monkeypatch):
    """Serve scripted responses and retain the requests made by a probe."""
    replies = deque()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, self.headers.get("Authorization"), body.decode()))
            status, payload, headers = replies.popleft() if replies else (599, b"Unexpected request", {})
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = respond

        def log_message(self, *args):
            pass

    def reply(payload, status=200, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        replies.append((status, body, headers or {}))

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(runs, "GARMIN", url + "/garmin")
        monkeypatch.setattr(runs, "STRAVA", url + "/strava")
        try:
            yield SimpleNamespace(reply=reply, calls=calls, url=url, port=server.server_port)
        finally:
            server.shutdown()
            thread.join()
