import hashlib

import pytest

from app.metainfo import InvalidTorrent, inspect_torrent
from conftest import bencode, torrent_bytes


def test_valid_metainfo_hash_is_exact():
    data = torrent_bytes()
    result = inspect_torrent(data)
    assert result["name"] == "hello.txt" and result["size"] == 11
    assert result["hash"] == hashlib.sha1(data[7:-1]).hexdigest()


@pytest.mark.parametrize(
    "name",
    [b"..", b"/etc/passwd", b"C:secret", b"folder\\file", b"a\x00b", b"", b"\xff"],
)
def test_bad_names_rejected(name):
    with pytest.raises(InvalidTorrent):
        inspect_torrent(torrent_bytes(name=name))


@pytest.mark.parametrize(
    "path", [[b"..", b"secret"], [b"/etc"], [b"x\\y"], [], b"not a list"]
)
def test_bad_multifile_paths_rejected(path):
    with pytest.raises(InvalidTorrent):
        inspect_torrent(torrent_bytes(files=[{b"length": 11, b"path": path}]))


def test_symlinks_and_duplicate_paths_rejected():
    for files in [
        [{b"length": 11, b"path": [b"link"], b"attr": b"l"}],
        [{b"length": 5, b"path": [b"same"]}, {b"length": 6, b"path": [b"same"]}],
    ]:
        with pytest.raises(InvalidTorrent):
            inspect_torrent(torrent_bytes(files=files))


@pytest.mark.parametrize(
    "data",
    [b"", b"de", b"i0e", b"d4:info", b"l" * 100, b"d4:infod4:name1:xe", b"9999:x"],
)
def test_malformed_metainfo_rejected(data):
    with pytest.raises(InvalidTorrent):
        inspect_torrent(data)


def test_multifile_total():
    result = inspect_torrent(
        torrent_bytes(
            name=b"package",
            files=[
                {b"length": 5, b"path": [b"a"]},
                {b"length": 6, b"path": [b"sub", b"b"]},
            ],
        )
    )
    assert result["size"] == 11


def test_inconsistent_pieces_and_v2_only_rejected():
    for info in [
        {b"name": b"test", b"length": 99, b"piece length": 16, b"pieces": b"x" * 20},
        {b"name": b"test", b"meta version": 2, b"file tree": {}},
    ]:
        with pytest.raises(InvalidTorrent):
            inspect_torrent(bencode({b"info": info}))
