import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    password: str
    session_secret: str
    download_dir: Path
    rpc_url: str
    rpc_username: str
    rpc_password: str
    secure_cookie: bool = False
    reserve_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls):
        password = os.environ.get("ST_PASSWORD", "")
        secret = os.environ.get("ST_SESSION_SECRET", "")
        if len(password) < 12 or len(secret) < 32:
            raise RuntimeError(
                "Set ST_PASSWORD (12+ characters) and ST_SESSION_SECRET (32+ characters). Run python3 scripts/setup.py."
            )
        return cls(
            password=password,
            session_secret=secret,
            download_dir=Path(
                os.environ.get("ST_DOWNLOAD_DIR", "/data/downloads")
            ).resolve(),
            rpc_url=os.environ.get(
                "ST_RPC_URL", "http://transmission:9091/transmission/rpc"
            ),
            rpc_username=os.environ.get("ST_RPC_USERNAME", "simpletorrent"),
            rpc_password=os.environ.get("ST_RPC_PASSWORD", ""),
            secure_cookie=os.environ.get("ST_SECURE_COOKIE", "false").lower() == "true",
            reserve_bytes=int(os.environ.get("ST_RESERVE_MIB", "256")) * 1024 * 1024,
        )
