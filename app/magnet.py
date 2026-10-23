"""Bounded v1 magnet admission; never fetch URLs during validation."""

import base64
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.metainfo import InvalidTorrent

MAX_MAGNET = 4096


def inspect_magnet(value):
    uri = value.strip()
    try:
        if (
            not uri
            or len(value) > MAX_MAGNET
            or any(ord(c) < 32 or ord(c) == 127 for c in uri)
        ):
            raise ValueError()
        if re.search(r"%(?![0-9a-fA-F]{2})", uri):
            raise ValueError()
        url = urlsplit(uri)
        if url.scheme.lower() != "magnet" or url.netloc or url.path or url.fragment:
            raise ValueError()
        params = parse_qsl(
            url.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=64,
            errors="strict",
        )
        topics = [
            v for k, v in params if k == "xt" and v.lower().startswith("urn:btih:")
        ]
        if len(topics) != 1:
            raise ValueError()
        info_hash = topics[0][9:]
        if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
            info_hash = info_hash.lower()
        elif re.fullmatch(r"[a-zA-Z2-7]{32}", info_hash):
            info_hash = base64.b32decode(info_hash.upper()).hex()
        else:
            raise ValueError()
        name = next((v for k, v in params if k == "dn"), "")
        if any(ord(c) < 32 or ord(c) == 127 for _, v in params for c in v):
            raise ValueError()
        if any(k == "so" for k, _ in params):
            raise InvalidTorrent(
                "Выбор отдельных файлов в magnet пока не поддерживается."
            )
        trackers = [v for k, v in params if k == "tr"]
        if len(trackers) > 20:
            raise ValueError()
        for tracker in trackers:
            target = urlsplit(tracker)
            if (
                target.scheme not in ("http", "https", "udp")
                or not target.hostname
                or target.username
                or target.password
                or target.fragment
            ):
                raise ValueError()
            _port = target.port  # Validate malformed/out-of-range ports.
        # Only forward supported magnet fields. In particular, arbitrary exact
        # source/webseed URLs do not become HTTP requests by Transmission.
        canonical = [("xt", "urn:btih:" + info_hash)]
        if name:
            canonical.append(("dn", name[:200]))
        canonical.extend(("tr", tracker) for tracker in trackers)
        return {
            "hash": info_hash,
            "name": name[:200] or info_hash,
            "uri": "magnet:?" + urlencode(canonical),
        }
    except InvalidTorrent:
        raise
    except (ValueError, UnicodeError) as exc:
        raise InvalidTorrent(
            "Нужна magnet-ссылка с одним v1 btih (40 hex или 32 base32)."
        ) from exc
