"""Loopback-only browser fixture. Never imported by the production app."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from conftest import FakeEngine

from app.config import Settings
from app.main import create_app


def run():
    with tempfile.TemporaryDirectory(prefix="simpletorrent-browser-") as directory:
        root = Path(directory).resolve() / "downloads"
        engine = FakeEngine(root)
        settings = Settings(
            "browser-test-password",
            "s" * 64,
            root,
            "http://unused.invalid",
            "u",
            "p",
            reserve_bytes=0,
        )
        app = create_app(settings, engine)

        @app.post("/_test/reset")
        async def reset():
            engine.items.clear()
            return {"ok": True}

        @app.post("/_test/complete/{torrent_id}")
        async def complete(torrent_id: str):
            item = next(t for t in engine.items if t["hashString"] == torrent_id)
            path = Path(item["downloadDir"]) / "hello.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"hello world")
            item.update(
                name="hello.txt",
                metadataPercentComplete=1,
                totalSize=11,
                leftUntilDone=0,
                percentDone=1,
                status=6,
                files=[{"name": "hello.txt", "length": 11, "bytesCompleted": 11}],
            )
            return {"ok": True}

        uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")


if __name__ == "__main__":
    run()
