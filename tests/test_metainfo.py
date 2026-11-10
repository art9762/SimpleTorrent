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


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("variant", ["plain", "legacy", "utf8", "utf8-fallback"])
@pytest.mark.parametrize("child", [[b"folder"], [b"folder", b"child.txt"]])
def test_conflicting_paths_rejected_in_every_name_variant(reverse, variant, child):
    files = [
        {b"length": 5, b"path": [b"folder"]},
        {b"length": 6, b"path": child},
    ]
    if variant == "legacy":
        # Distinct preferred names must not hide conflicting legacy paths.
        for i, entry in enumerate(files):
            entry[b"path.utf-8"] = [f"translated-{i}.txt".encode()]
    elif variant in ("utf8", "utf8-fallback"):
        files[1][b"path.utf-8"] = files[1][b"path"]
        files[1][b"path"] = [b"different.txt"]
        if variant == "utf8":
            files[0][b"path.utf-8"] = files[0][b"path"]
            files[0][b"path"] = [b"another.txt"]
    if reverse:
        files.reverse()
    with pytest.raises(InvalidTorrent):
        inspect_torrent(torrent_bytes(files=files))


def test_shared_directories_and_distinct_name_variants_are_valid():
    files = [
        {
            b"length": 5,
            b"path": [b"folder", b"a.txt"],
            b"path.utf-8": [b"translated", b"a.txt"],
        },
        {
            b"length": 6,
            b"path": [b"folder", b"subfolder", b"b.txt"],
            b"path.utf-8": [b"translated", b"subfolder", b"b.txt"],
        },
    ]
    assert inspect_torrent(torrent_bytes(files=files))["size"] == 11


def test_legacy_and_utf8_trees_are_independent():
    files = [
        {b"length": 5, b"path": [b"a"], b"path.utf-8": [b"b"]},
        {b"length": 6, b"path": [b"b"], b"path.utf-8": [b"a"]},
    ]
    assert inspect_torrent(torrent_bytes(files=files))["size"] == 11


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
