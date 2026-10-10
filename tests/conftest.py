import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import EngineError
from app.main import create_app
from app.metainfo import inspect_torrent


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return (
            b"d"
            + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items()))
            + b"e"
        )
    raise TypeError(value)


def torrent_bytes(name=b"hello.txt", data=b"hello world", files=None):
    info = {
        b"name": name,
        b"piece length": 16384,
        b"pieces": b"".join(
            hashlib.sha1(data[i : i + 16384]).digest()
            for i in range(0, len(data), 16384)
        ),
    }
    if files is None:
        info[b"length"] = len(data)
    else:
        info[b"files"] = files
    return bencode({b"info": info})


class FakeEngine:
    def __init__(self, root):
        self.root = root
        self.items = []
        self.failed = False
        self.settings = {
            "speed-limit-down": 0,
            "speed-limit-down-enabled": False,
            "speed-limit-up": 1024,
            "speed-limit-up-enabled": True,
            "version": "3.00",
        }

    async def torrents(self, ids=None, files=False):
        if self.failed:
            raise EngineError("Торрент-сервис недоступен.")
        return [dict(t) for t in self.items if ids is None or t["hashString"] in ids]

    async def call(self, method, arguments=None):
        if self.failed:
            raise EngineError("Торрент-сервис недоступен.")
        args = arguments or {}
        if method == "session-stats":
            return {}
        if method == "session-get":
            return self.settings
        if method == "session-set":
            self.settings.update(args)
            return {}
        if method == "torrent-add":
            import base64

            meta = inspect_torrent(base64.b64decode(args["metainfo"]))
            if any(t["hashString"] == meta["hash"] for t in self.items):
                return {"torrent-duplicate": {"hashString": meta["hash"]}}
            t = {
                "id": len(self.items) + 1,
                "hashString": meta["hash"],
                "name": meta["name"],
                "status": 0,
                "percentDone": 0,
                "totalSize": meta["size"],
                "leftUntilDone": meta["size"],
                "rateDownload": 0,
                "rateUpload": 0,
                "eta": -1,
                "peersConnected": 0,
                "error": 0,
                "errorString": "",
                "addedDate": 1700000000,
                "downloadDir": args["download-dir"],
                "files": [],
            }
            self.items.append(t)
            return {"torrent-added": {"hashString": meta["hash"]}}
        if method in ("torrent-start", "torrent-stop"):
            for t in self.items:
                if t["hashString"] in args["ids"]:
                    t["status"] = 4 if method == "torrent-start" else 0
            return {}
        if method == "torrent-remove":
            for t in self.items:
                if t["hashString"] in args["ids"] and args.get("delete-local-data"):
                    import shutil

                    shutil.rmtree(t["downloadDir"], ignore_errors=True)
            self.items = [t for t in self.items if t["hashString"] not in args["ids"]]
            return {}
        raise AssertionError(method)

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "downloads"
    settings = Settings(
        password="private-password",
        session_secret="s" * 64,
        download_dir=root,
        rpc_url="http://localhost:9091/transmission/rpc",
        rpc_username="u",
        rpc_password="p",
        reserve_bytes=0,
    )
    engine = FakeEngine(root)
    with TestClient(create_app(settings, engine)) as client:
        yield client, engine, settings


@pytest.fixture
def authorized(setup):
    client, engine, settings = setup
    response = client.post("/api/login", json={"password": settings.password})
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return client, engine, settings


def add(client, data=None):
    return client.post(
        "/api/torrents",
        files={
            "file": (
                "sample.torrent",
                data or torrent_bytes(),
                "application/x-bittorrent",
            )
        },
    )


def completed(client, engine, root, name="hello.txt", content=b"hello world"):
    response = add(client, torrent_bytes(name.encode(), content))
    assert response.status_code == 201, response.text
    t = engine.items[-1]
    path = Path(t["downloadDir"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    t.update(
        leftUntilDone=0,
        percentDone=1,
        status=6,
        files=[{"name": name, "length": len(content), "bytesCompleted": len(content)}],
    )
    return t["hashString"], path
