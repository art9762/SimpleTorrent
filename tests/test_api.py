import io
import shutil
import zipfile
from collections import namedtuple

from conftest import add, completed, torrent_bytes


def test_authentication_and_cookie(setup):
    client, _, settings = setup
    assert client.get("/").status_code == 200
    assert client.get("/api/torrents").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"password": settings.password})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/session").json()["csrf"] == response.json()["csrf"]
    client.cookies.set("st_session", "tampered")
    assert client.get("/api/torrents").status_code == 401


def test_rate_limit_and_cross_origin(setup):
    client, _, settings = setup
    assert (
        client.post(
            "/api/login",
            json={"password": settings.password},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    for _ in range(8):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert (
        client.post("/api/login", json={"password": settings.password}).status_code
        == 429
    )


def test_csrf_required_and_logout(authorized):
    client, _, _ = authorized
    token = client.headers.pop("X-CSRF-Token")
    assert add(client).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert (
        client.post("/api/logout", headers={"Origin": "https://evil.test"}).status_code
        == 403
    )
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/torrents").status_code == 401


def test_upload_pause_resume_duplicate_and_delete(authorized):
    client, engine, _ = authorized
    response = add(client)
    assert response.status_code == 201
    torrent_id = response.json()["id"]
    listing = client.get("/api/torrents").json()
    assert listing["torrents"][0]["status"] == "downloading"
    assert listing["storage"]["reserved"] == 11
    assert add(client).status_code == 409
    assert client.post(f"/api/torrents/{torrent_id}/pause").status_code == 200
    assert engine.items[0]["status"] == 0
    assert client.post(f"/api/torrents/{torrent_id}/resume").status_code == 200
    assert engine.items[0]["status"] == 4
    assert client.delete(f"/api/torrents/{torrent_id}").status_code == 200
    assert client.get("/api/torrents").json()["torrents"] == []
    assert client.post(f"/api/torrents/{torrent_id}/pause").status_code == 404
    assert client.post("/api/torrents/bad/resume").status_code == 404


def test_invalid_and_oversized_upload(authorized):
    client, _, _ = authorized
    assert add(client, b"not a torrent").status_code == 422
    assert add(client, b"x" * (4 * 1024 * 1024 + 1)).status_code == 413
    # Multipart body capped before spooling, including without Content-Length.
    body = (
        [
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="huge.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'
        ]
        + [b"x" * 1024 * 1024] * 6
        + [b"\r\n--boundary--\r\n"]
    )
    assert (
        client.post(
            "/api/torrents",
            content=iter(body),
            headers={"Content-Type": "multipart/form-data; boundary=boundary"},
        ).status_code
        == 413
    )


def test_conflicting_file_paths_rejected_before_engine_add(authorized):
    client, engine, _ = authorized
    data = torrent_bytes(
        name=b"package",
        files=[
            {b"length": 5, b"path": [b"folder"]},
            {b"length": 6, b"path": [b"folder", b"child.txt"]},
        ],
    )
    response = add(client, data)
    assert response.status_code == 422, response.text
    assert "пут" in response.json()["detail"]
    assert engine.items == []


def test_disk_admission_reserves_pending_downloads(authorized, monkeypatch):
    client, _, settings = authorized
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(100, 90, 10))
    assert add(client).status_code == 507
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(100, 80, 20))
    assert add(client).status_code == 201
    assert add(client, torrent_bytes(name=b"second.txt")).status_code == 507


def test_file_download_ranges_archive_and_removal(authorized):
    client, engine, settings = authorized
    torrent_id, path = completed(client, engine, settings.download_dir, "Пример.txt")
    base = f"/api/torrents/{torrent_id}"
    assert client.get(base + "/files").json()["files"][0]["ready"]
    response = client.get(base + "/files/0/download")
    assert response.content == b"hello world"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    for range_header, expected in [
        ("bytes=0-4", b"hello"),
        ("bytes=6-", b"world"),
        ("bytes=-5", b"world"),
    ]:
        part = client.get(base + "/files/0/download", headers={"Range": range_header})
        assert part.status_code == 206
        assert part.content == expected
    for range_header in [
        "bytes=99-",
        "bytes=5-2",
        "bytes=0-1,3-4",
        "bytes=-0",
        "something",
    ]:
        assert (
            client.get(
                base + "/files/0/download", headers={"Range": range_header}
            ).status_code
            == 416
        )
    response = client.get(base + "/archive")
    assert response.status_code == 200, response.text
    assert len(response.content) == int(response.headers["content-length"])
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("Пример.txt") == b"hello world"
    assert client.delete(base + "?delete_data=true").status_code == 200
    assert not path.exists()


def test_unfinished_missing_and_symlink_file_denied(authorized, tmp_path):
    client, engine, settings = authorized
    torrent_id, path = completed(client, engine, settings.download_dir)
    base = f"/api/torrents/{torrent_id}"
    engine.items[0]["files"][0]["bytesCompleted"] = 4
    assert client.get(base + "/files/0/download").status_code == 409
    engine.items[0]["files"][0]["bytesCompleted"] = 11
    assert client.get(base + "/files/-1/download").status_code == 404
    path.unlink()
    assert client.get(base + "/files/0/download").status_code == 409
    secret = tmp_path / "secret"
    secret.write_bytes(b"hello world")
    path.symlink_to(secret)
    assert client.get(base + "/files/0/download").status_code == 403
    assert not client.get(base + "/files").json()["files"][0]["ready"]
    engine.items[0]["files"][0]["name"] = "../secret"
    assert client.get(base + "/files/0/download").status_code == 403


def test_empty_file_and_keep_data(authorized):
    client, engine, settings = authorized
    torrent_id, path = completed(client, engine, settings.download_dir, content=b"")
    base = f"/api/torrents/{torrent_id}"
    assert client.get(base + "/files/0/download").content == b""
    assert (
        client.get(
            base + "/files/0/download", headers={"Range": "bytes=0-"}
        ).status_code
        == 416
    )
    assert client.delete(base).status_code == 200
    assert path.exists()


def test_settings_health_and_security_headers(authorized):
    client, engine, _ = authorized
    assert client.get("/healthz").status_code == 200
    assert (
        client.put(
            "/api/settings", json={"download_kib": 500, "upload_kib": 0}
        ).status_code
        == 200
    )
    assert client.get("/api/settings").json()["upload_kib"] == 0
    assert client.get("/api/settings").json()["download_kib"] == 500
    assert (
        client.put(
            "/api/settings", json={"download_kib": -1, "upload_kib": 0}
        ).status_code
        == 422
    )
    response = client.get("/api/torrents")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    engine.failed = True
    assert client.get("/healthz").status_code == 503
    assert client.get("/api/torrents").status_code == 503


def test_external_torrent_cannot_be_managed(authorized):
    client, engine, _ = authorized
    torrent_id = add(client).json()["id"]
    engine.items[0]["downloadDir"] = "/etc"
    assert client.get("/api/torrents").json()["torrents"] == []
    assert (
        client.delete(f"/api/torrents/{torrent_id}?delete_data=true").status_code == 403
    )
