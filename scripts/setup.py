#!/usr/bin/env python3
"""Create an ignored .env with a private password and independent session/RPC keys."""

import argparse
import getpass
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Configure SimpleTorrent before docker compose up"
)
parser.add_argument(
    "--generate", action="store_true", help="Generate a password instead of prompting"
)
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    raise SystemExit(
        ".env already exists; edit it to change settings. Nothing was overwritten."
    )
if args.generate:
    password = secrets.token_urlsafe(18)
else:
    password = getpass.getpass("Choose an access password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords did not match")
if len(password) < 12 or any(c in password for c in "\n\r'\\"):
    raise SystemExit(
        "Use 12+ characters; single quotes, backslashes and line breaks are not supported in .env."
    )
os.umask(0o077)
with path.open("x") as file:
    file.write(
        f"ST_PASSWORD='{password}'\nST_SESSION_SECRET={secrets.token_hex(32)}\nST_RPC_PASSWORD={secrets.token_hex(24)}\nST_PORT=8080\nST_BIND=0.0.0.0\nST_RESERVE_MIB=256\nST_SECURE_COOKIE=false\n"
    )
print("Created .env. Start with: docker compose up -d --build")
if args.generate:
    print("Access password (save it): " + password)
