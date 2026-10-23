import base64
import shutil
import time
from collections import namedtuple
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import add, torrent_bytes

from app.engine import EngineError
from app.magnet import MAX_MAGNET, inspect_magnet
from app.metainfo import InvalidTorrent, inspect_torrent

HASH = "0123456789abcdef0123456789abcdef01234567"
URI = "magnet:?xt=urn:btih:" + HASH


def magnet(client, uri=URI):
    return client.post("/api/torrents/magnet", json={"uri": uri})


@pytest.mark.parametrize(
    "value",
    [HASH, HASH.upper(), base64.b32encode(bytes.fromhex(HASH)).decode().lower()],
)
def test_normalizes_v1_infohash(value):
    result = inspect_magnet(
        "magnet:?xt=urn:btih:"
        + value
        + "&dn=%D0%A2%D0%B5%D1%81%D1%82&tr=udp%3A%2F%2F127.0.0.1%3A9%2Fannounce"
    )
    assert result["uri"].startswith(URI + "&")  # Transmission parses xt without percent-decoding.
    assert result["hash"] == HASH
    assert result["name"] == "Тест"
    params = parse_qs(urlsplit(result["uri"]).query)
    assert params["xt"] == ["urn:btih:" + HASH]
    assert params["tr"] == ["udp://127.0.0.1:9/announce"]


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "http://example.invalid/test",
        "magnet://host?xt=urn:btih:" + HASH,
        "magnet:/path?xt=urn:btih:" + HASH,
        "magnet:?xt=urn:btmh:1220" + "a" * 64,
        URI + "&xt=urn:btih:" + HASH,
        URI + "#fragment",
        URI + "&dn=%xx",
        URI + "&dn=%FF",
        URI + "&dn=%00",
        URI + "&dn=%7f",
        URI + "\n&dn=test",
        "magnet:?xt=urn:btih:" + "g" * 40,
        "magnet:?xt=urn:btih:" + "0" * 32,
        URI + "&tr=file%3A%2F%2F%2Fetc%2Fpasswd",
        URI + "&tr=http%3A%2F%2Fu%3Ap%40127.0.0.1%2F",
        URI + "&tr=http%3A%2F%2F127.0.0.1%3A99999%2F",
        URI + "&so=1",
        URI + "&dn=" + "x" * MAX_MAGNET,
        URI + "&x=y" * 65,
        URI + "&tr=udp%3A%2F%2F127.0.0.1%3A9" * 21,
    ],
)
def test_invalid_links_are_bounded_and_rejected(uri):
    with pytest.raises(InvalidTorrent):
        inspect_magnet(uri)


def test_only_supported_fields_forwarded_and_hybrid_keeps_v1():
    result = inspect_magnet(
        URI
        + "&xt=urn:btmh:1220"
        + "a" * 64
        + "&xs=http%3A%2F%2F127.0.0.1%2Fmetadata&xl=1"
    )
    assert parse_qs(urlsplit(result["uri"]).query) == {"xt": ["urn:btih:" + HASH]}


def test_requires_auth_and_csrf(setup, authorized):
    client, _, _ = setup
    client.cookies.clear()
    assert magnet(client).status_code == 401
    response = client.post("/api/login", json={"password": "private-password"})
    client.headers.pop("X-CSRF-Token", None)
    assert magnet(client).status_code == 403
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    assert magnet(client, "not-magnet").status_code == 422
    assert magnet(client, URI + "x" * MAX_MAGNET).status_code == 422


def test_unknown_metadata_never_becomes_ready_or_downloadable(authorized):
    client, engine, settings = authorized
    response = magnet(client)
    assert response.status_code == 201, response.text
    assert response.json()["id"] == HASH
    args = next(args for method, args in engine.calls if method == "torrent-add")
    assert args["paused"] is True and args["filename"].startswith("magnet:?")
    assert args["download-dir"] == str(settings.download_dir / HASH)
    t = engine.items[0]
    t["percentDone"] = 1  # Placeholder zeros do not imply complete metadata.
    t["files"] = [{"name": "empty", "length": 0, "bytesCompleted": 0}]
    base = f"/api/torrents/{HASH}"
    listing = client.get("/api/torrents").json()
    item = listing["torrents"][0]
    assert item["status"] == "metadata" and not item["metadata_ready"]
    assert item["size"] is None and item["remaining"] is None
    assert listing["storage"]["pending_metadata"] == 1
    assert client.get(base + "/files").json()["files"] == []
    assert client.get(base + "/files/0/download").status_code == 409
    assert client.get(base + "/archive").status_code == 409
    assert client.post(base + "/pause").status_code == 200
    assert client.get("/api/torrents").json()["torrents"][0]["status"] == "paused"
    assert client.post(base + "/resume").status_code == 200
    t.pop("files")
    assert client.get(base + "/files").json()["files"] == []
    t.pop("metadataPercentComplete")
    assert not client.get("/api/torrents").json()["torrents"][0]["metadata_ready"]
    assert client.get(base + "/archive").status_code == 409
    assert client.delete(base).status_code == 200


def test_metadata_transition_preserves_file_readiness(authorized):
    client, engine, _ = authorized
    assert magnet(client).status_code == 201
    t = engine.items[0]
    t.update(
        metadataPercentComplete=1,
        totalSize=11,
        leftUntilDone=11,
        name="hello.txt",
        files=[{"name": "hello.txt", "length": 11, "bytesCompleted": 0}],
    )
    base = f"/api/torrents/{HASH}"
    assert client.get("/api/torrents").json()["storage"]["reserved"] == 11
    assert client.get("/api/torrents").json()["storage"]["pending_metadata"] == 0
    assert client.get(base + "/files/0/download").status_code == 409
    path = Path(t["downloadDir"]) / "hello.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"hello world")
    t.update(leftUntilDone=0, percentDone=1, status=6)
    t["files"][0]["bytesCompleted"] = 11
    assert client.get(base + "/files").json()["files"][0]["ready"]
    assert client.get(base + "/files/0/download").content == b"hello world"
    assert client.get(base + "/archive").status_code == 200
    t["files"] = None
    assert client.get(base + "/files").json()["files"] == []
    assert client.get(base + "/files/0/download").status_code == 404
    assert client.get(base + "/archive").status_code == 409


@pytest.mark.parametrize("first", ["file", "magnet"])
def test_duplicates_across_upload_types_and_base32(authorized, first):
    client, engine, _ = authorized
    data = torrent_bytes()
    info_hash = inspect_torrent(data)["hash"]
    uri = "magnet:?xt=urn:btih:" + base64.b32encode(bytes.fromhex(info_hash)).decode()
    requests = [lambda: add(client, data), lambda: magnet(client, uri)]
    if first == "magnet":
        requests.reverse()
    assert requests[0]().status_code == 201
    assert requests[1]().status_code == 409
    assert len(engine.items) == 1
    assert magnet(client, "magnet:?xt=urn:btih:" + info_hash.upper()).status_code == 409


def test_engine_errors_limits_and_duplicate_race(authorized, monkeypatch):
    client, engine, _ = authorized
    call = engine.call

    async def duplicate(method, arguments=None):
        return (
            {"torrent-duplicate": {"hashString": HASH}}
            if method == "torrent-add"
            else await call(method, arguments)
        )

    monkeypatch.setattr(engine, "call", duplicate)
    assert magnet(client).status_code == 409
    monkeypatch.setattr(engine, "call", call)
    engine.failed = True
    assert magnet(client).status_code == 503
    engine.failed = False
    engine.items = [{"hashString": f"{i:040x}", "leftUntilDone": 0} for i in range(200)]
    assert magnet(client).status_code == 409
    engine.items.clear()

    async def no_start(method, arguments=None):
        if method == "torrent-start":
            raise EngineError("test startup failure")
        return await call(method, arguments)

    monkeypatch.setattr(engine, "call", no_start)
    assert magnet(client).status_code == 201
    assert engine.items[0]["status"] == 0


def test_capacity_blocks_resume_and_guard_pauses_after_metadata(
    authorized, monkeypatch
):
    client, engine, _ = authorized
    Usage = namedtuple("Usage", "total used free")
    assert magnet(client).status_code == 201
    t = engine.items[0]
    t.update(metadataPercentComplete=1, totalSize=1000, leftUntilDone=1000)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(2000, 1900, 100))
    assert client.get("/api/torrents").json()["storage"]["low"]
    assert client.post(f"/api/torrents/{HASH}/resume").status_code == 507
    assert magnet(client, "magnet:?xt=urn:btih:" + "a" * 40).status_code == 507
    deadline = time.monotonic() + 6
    while t["status"] != 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert t["status"] == 0
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(2000, 0, 2000))
    assert client.post(f"/api/torrents/{HASH}/resume").status_code == 200
