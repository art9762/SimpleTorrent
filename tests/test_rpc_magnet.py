"""Real loopback HTTP RPC → Transmission adapter → API, with synthetic state.

The RPC peer is a test fixture, not a BitTorrent peer. No tracker or swarm is used.
"""

import asyncio
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from conftest import FakeEngine
from fastapi.testclient import TestClient
from test_magnet import HASH, URI

from app.config import Settings
from app.main import create_app


def test_magnet_lifecycle_over_loopback_rpc(tmp_path):
    root = tmp_path / "downloads"
    engine = FakeEngine(root)
    requests = []

    class RPC(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Basic dTpw"  # test u:p
            if self.headers.get("X-Transmission-Session-Id") != "local-session":
                self.send_response(409)
                self.send_header("X-Transmission-Session-Id", "local-session")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            requests.append(body)
            method, args = body["method"], body["arguments"]
            if method == "torrent-get":
                result = {
                    "torrents": asyncio.run(
                        engine.torrents(
                            args.get("ids"), files="files" in args["fields"]
                        )
                    )
                }
            else:
                result = asyncio.run(engine.call(method, args))
            response = json.dumps({"result": "success", "arguments": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RPC)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        "private-password",
        "s" * 64,
        root,
        f"http://127.0.0.1:{server.server_port}/transmission/rpc",
        "u",
        "p",
        reserve_bytes=0,
    )

    def login(client):
        client.headers["X-CSRF-Token"] = client.post(
            "/api/login", json={"password": settings.password}
        ).json()["csrf"]

    base = f"/api/torrents/{HASH}"
    try:
        with TestClient(create_app(settings)) as client:
            login(client)
            assert (
                client.post("/api/torrents/magnet", json={"uri": URI}).status_code
                == 201
            )
            assert (
                client.get("/api/torrents").json()["torrents"][0]["status"]
                == "metadata"
            )
            assert client.get(base + "/archive").status_code == 409
            assert client.post(base + "/pause").status_code == 200
        # A fresh API/HTTP client reads the same engine-owned queue.
        with TestClient(create_app(settings)) as client:
            login(client)
            assert (
                client.get("/api/torrents").json()["torrents"][0]["status"] == "paused"
            )
            assert client.post(base + "/resume").status_code == 200
            t = engine.items[0]
            t.update(
                metadataPercentComplete=1,
                name="hello.txt",
                totalSize=11,
                leftUntilDone=11,
                files=[{"name": "hello.txt", "length": 11, "bytesCompleted": 0}],
            )
            assert client.get(base + "/files/0/download").status_code == 409
            path = Path(t["downloadDir"]) / "hello.txt"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"hello world")
            t.update(leftUntilDone=0, percentDone=1)
            t["files"][0]["bytesCompleted"] = 11
            assert client.get(base + "/files/0/download").content == b"hello world"
            with zipfile.ZipFile(
                io.BytesIO(client.get(base + "/archive").content)
            ) as archive:
                assert archive.read("hello.txt") == b"hello world"
            assert client.delete(base + "?delete_data=true").status_code == 200
            assert not path.exists()
        added = next(r["arguments"] for r in requests if r["method"] == "torrent-add")
        assert added["paused"] is True and added["filename"].startswith("magnet:?")
        assert any(
            "metadataPercentComplete" in r["arguments"].get("fields", [])
            for r in requests
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
