import asyncio
import base64
import contextlib
import logging
import os
import re
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import anyio
import zipstream
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool

from app.config import Settings
from app.engine import EngineError, Transmission
from app.magnet import MAX_MAGNET, inspect_magnet
from app.metainfo import MAX_UPLOAD, InvalidTorrent, inspect_torrent
from app.security import COOKIE, SESSION_SECONDS, Security

log = logging.getLogger("simpletorrent")
STATIC = Path(__file__).parent / "static"


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Bound total multipart bytes before Starlette can spool them to disk."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD + 64 * 1024 if scope["path"] == "/api/torrents" else 8192
        detail = "Файл слишком большой. Максимум — 4 МиБ."
        if scope["path"] == "/api/torrents/magnet":
            limit = MAX_MAGNET * 6 + 128  # JSON escaping of a bounded Unicode URI.
            detail = "Magnet-ссылка слишком большая. Максимум — 4096 символов."
        headers = dict(scope["headers"])
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        if declared > limit:
            return await JSONResponse({"detail": detail}, 413)(scope, receive, send)
        count, exceeded = 0, False

        async def bounded_receive():
            nonlocal count, exceeded
            message = await receive()
            count += len(message.get("body", b""))
            if count > limit:
                exceeded = True
                # BaseException is deliberately avoided; propagate past multipart's
                # HTTPException conversion using our own exception.
                raise BodyTooLarge()
            return message

        async def bounded_send(message):
            if exceeded:
                return
            await send(message)

        try:
            await self.app(scope, bounded_receive, bounded_send)
        except BodyTooLarge:
            exceeded = True
        if exceeded:
            await JSONResponse({"detail": detail}, 413)(scope, receive, send)


class Login(BaseModel):
    password: str = Field(max_length=512)


class Limits(BaseModel):
    download_kib: int = Field(ge=0, le=1000000)
    upload_kib: int = Field(ge=0, le=1000000)


class Magnet(BaseModel):
    uri: str = Field(min_length=1, max_length=MAX_MAGNET)


def create_app(settings=None, engine=None):
    settings = settings or Settings.from_env()
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    engine = engine or Transmission(settings)
    security = Security(settings)
    operations = asyncio.Lock()
    transfers = Counter()

    def metadata_ready(t):
        return t.get("metadataPercentComplete", 0) >= 1

    async def disk_guard():
        while True:
            try:
                async with operations:
                    torrents = await engine.torrents()
                    if storage(torrents)["low"]:
                        ids = [
                            t["hashString"]
                            for t in torrents
                            if (not metadata_ready(t) or t["leftUntilDone"] > 0)
                            and t["status"] != 0
                        ]
                        if ids:
                            await engine.call("torrent-stop", {"ids": ids})
                            log.warning("Downloads paused: disk reserve reached")
            except (EngineError, OSError):
                log.warning("Disk monitor could not reach storage or engine")
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(disk_guard())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await engine.close()

    app = FastAPI(
        title="SimpleTorrent",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(EngineError)
    async def engine_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/healthz")
    async def health():
        await engine.call("session-stats")
        return {"status": "ok"}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        security.check_password(request, body.password)
        cookie, csrf = security.issue()
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
        )
        return {"csrf": csrf}

    @app.get("/api/session")
    async def session(auth=Depends(security.authenticate)):
        return {"csrf": auth["csrf"]}

    @app.post("/api/logout")
    async def logout(response: Response, auth=Depends(security.authenticate)):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    def storage(torrents):
        usage = shutil.disk_usage(settings.download_dir)
        reserved = sum(t["leftUntilDone"] for t in torrents)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "reserved": reserved,
            "available": max(0, usage.free - reserved - settings.reserve_bytes),
            "reserve": settings.reserve_bytes,
            "low": usage.free - reserved < settings.reserve_bytes,
            "pending_metadata": sum(not metadata_ready(t) for t in torrents),
        }

    def require_capacity(torrents, additional=0):
        disk = storage(torrents)
        if disk["free"] - disk["reserved"] - settings.reserve_bytes < additional:
            raise HTTPException(
                507,
                "Не хватает места с учётом очереди. Освободите диск или удалите ненужные загрузки.",
            )

    def public(t):
        known = metadata_ready(t)
        status = (
            "ready"
            if t["leftUntilDone"] == 0 and known
            else {
                0: "paused",
                1: "queued",
                2: "checking",
                3: "queued",
                4: "downloading",
                5: "ready",
                6: "ready",
            }.get(t["status"], "queued")
        )
        if t.get("error"):
            status = "error"
        elif not known:
            status = "paused" if t["status"] == 0 else "metadata"
        return {
            "id": t["hashString"],
            "name": t["name"],
            "status": status,
            "paused": t["status"] == 0,
            "progress": t["percentDone"] if known else 0,
            "size": t["totalSize"] if known else None,
            "remaining": t["leftUntilDone"] if known else None,
            "metadata_ready": known,
            "metadata_progress": t.get("metadataPercentComplete", 0),
            "download_speed": t["rateDownload"],
            "upload_speed": t["rateUpload"],
            "eta": t["eta"],
            "peers": t["peersConnected"],
            "error": t.get("errorString", ""),
            "added_at": t["addedDate"],
            "stalled": t.get("isStalled", False),
        }

    async def get_torrent(torrent_id, files=False):
        if not re.fullmatch(r"[0-9a-f]{40}", torrent_id):
            raise HTTPException(404, "Загрузка не найдена.")
        result = await engine.torrents([torrent_id], files=files)
        if not result:
            raise HTTPException(404, "Загрузка не найдена.")
        torrent = result[0]
        # Only manage isolated directories created by this application.
        if Path(torrent["downloadDir"]).resolve() != settings.download_dir / torrent_id:
            raise HTTPException(403, "Загрузка находится вне каталога SimpleTorrent.")
        return torrent

    @app.get("/api/torrents")
    async def list_torrents(auth=Depends(security.authenticate)):
        torrents = await engine.torrents()
        torrents = [
            t
            for t in torrents
            if Path(t["downloadDir"]).resolve()
            == settings.download_dir / t["hashString"]
        ]
        return {
            "torrents": [
                public(t)
                for t in sorted(torrents, key=lambda t: t["addedDate"], reverse=True)
            ],
            "storage": storage(torrents),
            "download_speed": sum(t["rateDownload"] for t in torrents),
            "upload_speed": sum(t["rateUpload"] for t in torrents),
        }

    @app.post("/api/torrents", status_code=201)
    async def add_torrent(file: UploadFile, auth=Depends(security.authenticate)):
        try:
            data = await file.read(MAX_UPLOAD + 1)
        finally:
            await file.close()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Максимальный размер .torrent — 4 МиБ.")
        try:
            metadata = inspect_torrent(data)
        except InvalidTorrent as exc:
            raise HTTPException(422, str(exc)) from exc
        async with operations:
            existing = await engine.torrents()
            if any(t["hashString"] == metadata["hash"] for t in existing):
                raise HTTPException(409, "Этот торрент уже есть в списке.")
            require_capacity(existing, metadata["size"])
            if len(existing) >= 200:
                raise HTTPException(
                    409, "Максимум 200 загрузок. Удалите старые из списка."
                )
            args = {
                "metainfo": base64.b64encode(data).decode(),
                "paused": True,
                "download-dir": str(settings.download_dir / metadata["hash"]),
            }
            result = await engine.call("torrent-add", args)
            if "torrent-duplicate" in result:
                raise HTTPException(409, "Этот торрент уже есть в списке.")
            torrent_id = result["torrent-added"]["hashString"]
            try:
                await engine.call("torrent-start", {"ids": [torrent_id]})
            except EngineError:
                # Preserve the paused torrent: it is visible and can be resumed.
                log.warning("New torrent is paused because start failed")
            return {"id": torrent_id, "name": metadata["name"]}

    @app.post("/api/torrents/{torrent_id}/pause")
    async def pause(torrent_id: str, auth=Depends(security.authenticate)):
        async with operations:
            await get_torrent(torrent_id)
            await engine.call("torrent-stop", {"ids": [torrent_id]})
        return {"ok": True}

    @app.post("/api/torrents/magnet", status_code=201)
    async def add_magnet(body: Magnet, auth=Depends(security.authenticate)):
        try:
            metadata = inspect_magnet(body.uri)
        except InvalidTorrent as exc:
            raise HTTPException(422, str(exc)) from exc
        async with operations:
            existing = await engine.torrents()
            if any(t["hashString"].lower() == metadata["hash"] for t in existing):
                raise HTTPException(409, "Этот торрент уже есть в списке.")
            if len(existing) >= 200:
                raise HTTPException(
                    409, "Максимум 200 загрузок. Удалите старые из списка."
                )
            require_capacity(existing)
            result = await engine.call(
                "torrent-add",
                {
                    "filename": metadata["uri"],
                    "paused": True,
                    "download-dir": str(settings.download_dir / metadata["hash"]),
                },
            )
            if "torrent-duplicate" in result:
                raise HTTPException(409, "Этот торрент уже есть в списке.")
            torrent_id = result["torrent-added"]["hashString"]
            try:
                await engine.call("torrent-start", {"ids": [torrent_id]})
            except EngineError:
                log.warning("New magnet is paused because start failed")
            return {"id": torrent_id, "name": metadata["name"]}

    @app.post("/api/torrents/{torrent_id}/resume")
    async def resume(torrent_id: str, auth=Depends(security.authenticate)):
        async with operations:
            torrent = await get_torrent(torrent_id)
            if not metadata_ready(torrent) or torrent["leftUntilDone"]:
                require_capacity(await engine.torrents())
            await engine.call("torrent-start", {"ids": [torrent_id]})
        return {"ok": True}

    @app.delete("/api/torrents/{torrent_id}")
    async def delete(
        torrent_id: str, delete_data: bool = False, auth=Depends(security.authenticate)
    ):
        async with operations:
            await get_torrent(torrent_id)
            if transfers[torrent_id]:
                raise HTTPException(
                    409, "Файлы сейчас передаются. Дождитесь завершения скачивания."
                )
            await engine.call(
                "torrent-remove",
                {"ids": [torrent_id], "delete-local-data": delete_data},
            )
        return {"ok": True}

    def safe_file(t, index):
        if not metadata_ready(t):
            raise HTTPException(
                409, "Получаем метаданные. Список файлов пока неизвестен."
            )
        files = t.get("files") or []
        if index < 0 or index >= len(files):
            raise HTTPException(404, "Файл не найден.")
        entry = files[index]
        relative = PurePosixPath(entry["name"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in entry["name"]:
            raise HTTPException(403, "Небезопасный путь файла.")
        root = settings.download_dir / t["hashString"]
        path = root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root.resolve()):
            raise HTTPException(403, "Небезопасный путь файла.")
        current = path
        while current != settings.download_dir:
            if current.is_symlink():
                raise HTTPException(403, "Символические ссылки не поддерживаются.")
            current = current.parent
        if (
            entry["bytesCompleted"] != entry["length"]
            or not path.is_file()
            or path.stat().st_size != entry["length"]
        ):
            raise HTTPException(409, "Этот файл ещё не готов или отсутствует на диске.")
        return path, entry

    @app.get("/api/torrents/{torrent_id}/files")
    async def files(torrent_id: str, auth=Depends(security.authenticate)):
        t = await get_torrent(torrent_id, files=True)
        result = []
        for i, entry in enumerate((t.get("files") or []) if metadata_ready(t) else []):
            ready = False
            if entry["bytesCompleted"] == entry["length"]:
                try:
                    safe_file(t, i)
                    ready = True
                except HTTPException:
                    pass
            result.append(
                {
                    "index": i,
                    "name": entry["name"],
                    "size": entry["length"],
                    "completed": entry["bytesCompleted"],
                    "ready": ready,
                }
            )
        return {"torrent": public(t), "files": result}

    def disposition(name):
        return "attachment; filename*=UTF-8''" + quote(name, safe="")

    @app.get("/api/torrents/{torrent_id}/files/{index}/download")
    async def download(
        torrent_id: str,
        index: int,
        request: Request,
        auth=Depends(security.authenticate),
    ):
        async with operations:
            t = await get_torrent(torrent_id, files=True)
            path, entry = safe_file(t, index)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                raise HTTPException(409, "Файл недоступен. Обновите список.") from exc
            length = os.fstat(fd).st_size
            start, end, code = 0, length - 1, 200
            headers = {
                "Content-Disposition": disposition(path.name),
                "Accept-Ranges": "bytes",
            }
            range_header = request.headers.get("range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                try:
                    if not match or not any(match.groups()) or not length:
                        raise ValueError()
                    a, b = match.groups()
                    if a:
                        start = int(a)
                        end = min(int(b), length - 1) if b else length - 1
                    else:
                        suffix = int(b)
                        if not suffix:
                            raise ValueError()
                        start = max(0, length - suffix)
                    if start >= length or end < start:
                        raise ValueError()
                except ValueError:
                    os.close(fd)
                    return Response(
                        status_code=416, headers={"Content-Range": f"bytes */{length}"}
                    )
                code = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{length}"
            headers["Content-Length"] = str(max(0, end - start + 1))
            transfers[torrent_id] += 1

        async def stream():
            try:
                with os.fdopen(fd, "rb") as source:
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = await anyio.to_thread.run_sync(
                            source.read, min(1024 * 1024, remaining)
                        )
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
            finally:
                transfers[torrent_id] -= 1
                if not transfers[torrent_id]:
                    transfers.pop(torrent_id, None)

        return StreamingResponse(
            stream(),
            status_code=code,
            media_type="application/octet-stream",
            headers=headers,
        )

    @app.get("/api/torrents/{torrent_id}/archive")
    async def archive(torrent_id: str, auth=Depends(security.authenticate)):
        async with operations:
            t = await get_torrent(torrent_id, files=True)
            if not metadata_ready(t):
                raise HTTPException(
                    409, "Получаем метаданные. Список файлов пока неизвестен."
                )
            if not t.get("files"):
                raise HTTPException(
                    409, "Список файлов ещё не готов. Обновите страницу позже."
                )
            if t["leftUntilDone"]:
                raise HTTPException(409, "Дождитесь завершения всей загрузки.")
            archive = zipstream.ZipStream(
                compress_type=zipstream.ZIP_STORED, sized=True
            )
            for i in range(len(t["files"])):
                path, entry = safe_file(t, i)
                archive.add_path(path, arcname=entry["name"])
            size = len(archive)
            transfers[torrent_id] += 1

        async def stream():
            try:
                async for chunk in iterate_in_threadpool(iter(archive)):
                    yield chunk
            finally:
                transfers[torrent_id] -= 1
                if not transfers[torrent_id]:
                    transfers.pop(torrent_id, None)

        return StreamingResponse(
            stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition": disposition(t["name"] + ".zip"),
                "Content-Length": str(size),
            },
        )

    @app.get("/api/settings")
    async def get_settings(auth=Depends(security.authenticate)):
        result = await engine.call("session-get")
        return {
            "download_kib": result["speed-limit-down"]
            if result["speed-limit-down-enabled"]
            else 0,
            "upload_kib": result["speed-limit-up"]
            if result["speed-limit-up-enabled"]
            else 0,
            "engine_version": result["version"],
        }

    @app.put("/api/settings")
    async def set_settings(body: Limits, auth=Depends(security.authenticate)):
        await engine.call(
            "session-set",
            {
                "speed-limit-down": body.download_kib,
                "speed-limit-down-enabled": body.download_kib > 0,
                "speed-limit-up": body.upload_kib,
                "speed-limit-up-enabled": body.upload_kib > 0,
            },
        )
        return {"ok": True}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
