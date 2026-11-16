from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

import runs


def get_health(port):
    with closing(HTTPConnection("127.0.0.1", port, timeout=1)) as client:
        client.request("GET", "/health")
        with client.getresponse() as response:
            return response.status, json.load(response)


@contextmanager
def serve_database(path):
    # Reserve the socket until Uvicorn inherits it so parallel tests never
    # compete for a fixed port. Use the documented app entry point.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        fd = listener.fileno()
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--fd", str(fd), "--log-level", "warning"],
            cwd=Path(__file__).resolve().parents[1], pass_fds=(fd,),
            env={"RUNS_DB_PATH": str(path), "RUNS_GARMIN_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    get_health(port)
                    break
                except OSError:
                    if server.poll() is not None:
                        pytest.fail("Uvicorn exited before serving /health")
                    time.sleep(0.02)
            else:
                pytest.fail("Uvicorn never served /health")
            yield port
        finally:
            server.terminate()
            server.wait(timeout=5)


def test_health_http_and_cli_for_empty_populated_broken_and_missing_storage(cli, tmp_path, monkeypatch):
    db_path = tmp_path / "test #1?.sqlite3"
    sqlite3.connect(db_path).close()
    # Reading credentials would fail in this test, even with a provider configured.
    (tmp_path / ".env").mkdir()
    with serve_database(db_path) as port:
        monkeypatch.setattr(runs, "HEALTH_PORT", port)
        assert get_health(port) == (200, {"status": "ok"})
        code, out, err = cli("health")
        assert code == 0 and "SQLite is readable" in out and not err
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE sample (id INTEGER)")
        db_path.chmod(0o400)
        assert get_health(port) == (200, {"status": "ok"})
        db_path.chmod(0o600)
        db_path.write_bytes(b"private-invalid-database" * 100)
        status, body = get_health(port)
        assert status == 503 and str(db_path) not in str(body)
        code, out, err = cli("health")
        assert code != 0 and "503" in err and "SQLite read permissions" in err
        assert "private-invalid" not in out + err and str(db_path) not in out + err
        db_path.write_bytes(b"x")
        assert get_health(port)[0] == 503
        db_path.unlink()
        assert get_health(port)[0] == 503 and not db_path.exists()
        db_path.mkdir()  # Directory where a readable database file is required.
        assert get_health(port)[0] == 503
        db_path.rmdir()
        os.mkfifo(db_path)
        assert get_health(port)[0] == 503
        db_path.unlink()
        db_path.symlink_to(os.devnull)
        assert get_health(port)[0] == 503

    code, out, err = cli("health")
    assert code != 0 and "connection failed" in err and "Start FastAPI" in err


@pytest.mark.parametrize(("status", "payload", "message"), [
    (302, b"private-error-body", "HTTP 302"),
    (204, b"", "HTTP 204"),
    (500, b"private-error-body", "HTTP 500"),
    (200, b"private-error-body", "unexpected response"),
    (200, {"status": "unhealthy"}, "unexpected response"),
])
def test_health_checks_status_and_body_and_never_follows_redirects(status, payload, message, cli, upstream, monkeypatch):
    monkeypatch.setattr(runs, "HEALTH_PORT", upstream.port)
    upstream.reply(payload, status, {"Location": upstream.url + "/garmin"})
    code, out, err = cli("health")
    assert code != 0 and message in err and "retry" in err
    assert "private-error-body" not in out + err
    assert upstream.calls == [("GET", "/health", None, "")]


def test_health_timeout(cli, monkeypatch):
    # TCP handshake succeeds but the listening process never replies.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(runs, "HEALTH_PORT", listener.getsockname()[1])
        monkeypatch.setattr(runs, "HEALTH_TIMEOUT", 0.05)
        code, out, err = cli("health")
    assert code != 0 and "timed out" in err and "retry" in err


def test_health_uses_documented_loopback_address(cli, upstream, monkeypatch):
    def connect(host, port, timeout):
        assert (host, port) == ("127.0.0.1", 8001)
        return HTTPConnection(host, upstream.port, timeout=timeout)

    monkeypatch.setattr(runs, "HTTPConnection", connect)
    upstream.reply({"status": "ok"})
    assert cli("health")[0] == 0
    assert upstream.calls == [("GET", "/health", None, "")]
