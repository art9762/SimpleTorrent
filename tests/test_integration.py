"""Real local webseed → Transmission → authenticated HTTP file/ZIP downloads.

No public tracker, external swarm, or copyrighted fixture is used.
Run ST_INTEGRATION=1 pytest -q tests/test_integration.py with transmission-daemon on PATH.
"""

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import bencode

pytestmark = pytest.mark.skipif(
    os.environ.get("ST_INTEGRATION") != "1" or not shutil.which("transmission-daemon"),
    reason="Opt-in: requires a real Transmission daemon",
)
CONTENT = b"SimpleTorrent local integration fixture.\n" * 16000


class Webseed(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/announce"):
            data = bencode({b"interval": 60, b"peers": b""})
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path != "/fixture.txt":
            self.send_error(404)
            return
        start, end = 0, len(CONTENT) - 1
        if self.headers.get("Range"):
            start_s, end_s = self.headers["Range"][6:].split("-")
            start = int(start_s)
            end = min(int(end_s), end) if end_s else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(CONTENT)}")
        else:
            self.send_response(200)
        data = CONTENT[start : end + 1]
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_daemon(config, port):
    process = subprocess.Popen(
        [
            "transmission-daemon",
            "--foreground",
            "--config-dir",
            str(config),
            "--log-error",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("Transmission exited before startup")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return process
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    raise AssertionError("Transmission did not start")


def stop_daemon(process):
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def test_real_download_file_archive_and_restart(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    rpc_port = free_port()
    settings = Settings(
        password="integration-password",
        session_secret="a" * 64,
        download_dir=root,
        rpc_url=f"http://127.0.0.1:{rpc_port}/transmission/rpc",
        rpc_username="tester",
        rpc_password="rpc-password",
        reserve_bytes=0,
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "rpc-enabled": True,
                "rpc-bind-address": "127.0.0.1",
                "rpc-port": rpc_port,
                "rpc-authentication-required": True,
                "rpc-username": settings.rpc_username,
                "rpc-password": settings.rpc_password,
                "rpc-whitelist-enabled": False,
                "rpc-host-whitelist-enabled": False,
                "download-dir": str(root),
                "dht-enabled": False,
                "pex-enabled": False,
                "lpd-enabled": False,
                "port-forwarding-enabled": False,
                "peer-port": free_port(),
                "start-added-torrents": False,
                "speed-limit-up-enabled": False,
            }
        )
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Webseed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = start_daemon(config, rpc_port)
    try:
        port = server.server_address[1]
        piece_length = 16384
        info = {
            b"name": b"fixture.txt",
            b"length": len(CONTENT),
            b"piece length": piece_length,
            b"pieces": b"".join(
                hashlib.sha1(CONTENT[i : i + piece_length]).digest()
                for i in range(0, len(CONTENT), piece_length)
            ),
        }
        torrent = bencode(
            {
                b"announce": f"http://127.0.0.1:{port}/announce".encode(),
                b"url-list": [f"http://127.0.0.1:{port}/fixture.txt".encode()],
                b"info": info,
            }
        )
        with TestClient(create_app(settings)) as client:
            login = client.post("/api/login", json={"password": settings.password})
            client.headers["X-CSRF-Token"] = login.json()["csrf"]
            assert client.get("/healthz").status_code == 200
            response = client.post(
                "/api/torrents", files={"file": ("fixture.torrent", torrent)}
            )
            assert response.status_code == 201, response.text
            torrent_id = response.json()["id"]
            deadline = time.monotonic() + 65
            while time.monotonic() < deadline:
                state = client.get("/api/torrents").json()["torrents"][0]
                if state["remaining"] == 0:
                    break
                time.sleep(0.5)
            else:
                pytest.fail(f"Local webseed download did not complete: {state}")
            response = client.get(f"/api/torrents/{torrent_id}/files/0/download")
            assert response.status_code == 200
            assert (
                hashlib.sha256(response.content).digest()
                == hashlib.sha256(CONTENT).digest()
            )
            archive = client.get(f"/api/torrents/{torrent_id}/archive")
            assert archive.status_code == 200, archive.text
            with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
                assert z.read("fixture.txt") == CONTENT
            assert client.post(f"/api/torrents/{torrent_id}/pause").status_code == 200
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                paused = client.get("/api/torrents").json()["torrents"][0]
                if paused["paused"]:
                    break
                time.sleep(0.1)
            assert paused["paused"], paused
        stop_daemon(process)
        process = start_daemon(config, rpc_port)
        with TestClient(create_app(settings)) as client:
            client.headers["X-CSRF-Token"] = client.post(
                "/api/login", json={"password": settings.password}
            ).json()["csrf"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = client.get("/api/torrents").json()["torrents"]
                if result and result[0]["paused"]:
                    break
                time.sleep(0.1)
            assert result[0]["id"] == torrent_id
            assert result[0]["remaining"] == 0 and result[0]["paused"]
            assert (
                client.get(f"/api/torrents/{torrent_id}/files/0/download").content
                == CONTENT
            )
            assert (
                client.delete(
                    f"/api/torrents/{torrent_id}?delete_data=true"
                ).status_code
                == 200
            )
            deadline = time.monotonic() + 5
            while (
                root / torrent_id / "fixture.txt"
            ).exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not (root / torrent_id / "fixture.txt").exists()
    finally:
        stop_daemon(process)
        server.shutdown()
        server.server_close()
