"""Generate managed RPC settings, preserving queue and speed limits on restart."""

import json
import os
from pathlib import Path

config = Path(os.environ.get("ST_ENGINE_CONFIG", "/data/config"))
config.mkdir(parents=True, exist_ok=True)
path = config / "settings.json"
settings = (
    json.loads(path.read_text())
    if path.exists()
    else {
        "cache-size-mb": 16,
        "download-queue-enabled": True,
        "download-queue-size": 3,
        "peer-limit-global": 120,
        "peer-limit-per-torrent": 40,
        "speed-limit-up": 1024,
        "speed-limit-up-enabled": True,
        "ratio-limit": 1.0,
        "ratio-limit-enabled": True,
    }
)
password = os.environ.get("ST_RPC_PASSWORD", "")
if len(password) < 12:
    raise SystemExit("ST_RPC_PASSWORD must have at least 12 characters")
settings.update(
    {
        "rpc-enabled": True,
        "rpc-authentication-required": True,
        "rpc-username": os.environ.get("ST_RPC_USERNAME", "simpletorrent"),
        "rpc-password": password,
        "rpc-bind-address": "0.0.0.0",
        "rpc-port": int(os.environ.get("ST_ENGINE_PORT", "9091")),
        "rpc-whitelist-enabled": False,
        "rpc-host-whitelist-enabled": True,
        "rpc-host-whitelist": "transmission,localhost",
        "download-dir": os.environ.get("ST_DOWNLOAD_DIR", "/data/downloads"),
        "incomplete-dir-enabled": False,
        "rename-partial-files": True,
        "start-added-torrents": False,
        "port-forwarding-enabled": False,
        "peer-port": 51413,
        "umask": 18,
    }
)
# Store credentials only in the private config file; Transmission hashes its RPC
# password when it saves settings. Never print environment or settings.
os.umask(0o077)
path.write_text(json.dumps(settings, indent=2))
path.chmod(0o600)
os.execvp(
    "transmission-daemon",
    ["transmission-daemon", "--foreground", "--config-dir", str(config), "--log-error"],
)
