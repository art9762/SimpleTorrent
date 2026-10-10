"""Bounded bencode parser and preflight path validation for v1/hybrid torrents."""

import hashlib
from pathlib import PurePosixPath

MAX_UPLOAD = 4 * 1024 * 1024
MAX_FILES = 10000


class InvalidTorrent(ValueError):
    pass


class Decoder:
    def __init__(self, data):
        self.data, self.pos, self.nodes = data, 0, 0
        self.info_span = None

    def read(self, depth=0):
        self.nodes += 1
        if depth > 32 or self.nodes > 100000 or self.pos >= len(self.data):
            raise InvalidTorrent("Повреждённый или слишком сложный .torrent.")
        token = self.data[self.pos : self.pos + 1]
        if token == b"i":
            end = self.data.index(b"e", self.pos)
            value = int(self.data[self.pos + 1 : end])
            self.pos = end + 1
            return value
        if token in (b"d", b"l"):
            self.pos += 1
            result = {} if token == b"d" else []
            while self.data[self.pos : self.pos + 1] != b"e":
                if token == b"d":
                    key = self.read(depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise InvalidTorrent("Некорректный словарь .torrent.")
                    start = self.pos
                    result[key] = self.read(depth + 1)
                    if depth == 0 and key == b"info":
                        self.info_span = (start, self.pos)
                else:
                    result.append(self.read(depth + 1))
            self.pos += 1
            return result
        if token.isdigit():
            end = self.data.index(b":", self.pos)
            length = int(self.data[self.pos : end])
            start = end + 1
            if length < 0 or start + length > len(self.data):
                raise InvalidTorrent("Неверная длина данных .torrent.")
            self.pos = start + length
            return self.data[start : self.pos]
        raise InvalidTorrent("Это не .torrent файл.")


def component(value):
    if not isinstance(value, bytes) or not value:
        raise InvalidTorrent("В торренте есть пустое имя файла.")
    text = value.decode("utf-8", errors="strict")
    if (
        text in (".", "..")
        or any(c in text for c in "/\\:\x00")
        or any(ord(c) < 32 for c in text)
    ):
        raise InvalidTorrent("В торренте есть небезопасный путь.")
    return text


def inspect_torrent(data):
    try:
        decoder = Decoder(data)
        meta = decoder.read()
        if decoder.pos != len(data) or not isinstance(meta, dict):
            raise InvalidTorrent("Некорректный .torrent файл.")
        info = meta[b"info"]
        name = component(info.get(b"name.utf-8", info[b"name"]))
        # Validate both legacy and UTF-8 names; Transmission may choose either.
        component(info[b"name"])
        pieces = info.get(b"pieces")
        if (
            not isinstance(pieces, bytes)
            or len(pieces) % 20
            or not isinstance(info.get(b"piece length"), int)
            or info[b"piece length"] <= 0
        ):
            raise InvalidTorrent(
                "Нужен обычный v1 или гибридный .torrent; чистый v2 пока не поддерживается."
            )
        entries = info.get(b"files", [{b"length": info.get(b"length"), b"path": []}])
        if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
            raise InvalidTorrent("Торрент должен содержать от 1 до 10 000 файлов.")
        size, seen = 0, set()
        for entry in entries:
            length = entry[b"length"]
            if type(length) is not int or length < 0:
                raise InvalidTorrent("Некорректный размер файла.")
            if b"l" in entry.get(b"attr", b"") or b"symlink path" in entry:
                raise InvalidTorrent(
                    "Символические ссылки в торренте не поддерживаются."
                )
            for key in (b"path", b"path.utf-8"):
                if key in entry:
                    path = entry[key]
                    if not isinstance(path, list) or (b"files" in info and not path):
                        raise InvalidTorrent("Некорректный путь файла.")
                    for part in path:
                        component(part)
            path = str(
                PurePosixPath(
                    name,
                    *(component(p) for p in entry.get(b"path.utf-8", entry[b"path"])),
                )
            )
            if path in seen:
                raise InvalidTorrent("В торренте повторяется имя файла.")
            seen.add(path)
            size += length
        expected_pieces = (size + info[b"piece length"] - 1) // info[b"piece length"]
        if len(pieces) // 20 != expected_pieces:
            raise InvalidTorrent("Некорректная таблица частей .torrent.")
        start, end = decoder.info_span
        return {
            "name": name,
            "size": size,
            "hash": hashlib.sha1(data[start:end]).hexdigest(),
        }
    except InvalidTorrent:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        UnicodeError,
    ) as exc:
        raise InvalidTorrent("Повреждённый .torrent файл.") from exc
