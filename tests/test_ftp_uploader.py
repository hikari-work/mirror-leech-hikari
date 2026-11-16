"""Tests for the FTP uploader.

The server is faked at the ``ftplib`` seam -- a class that answers the same
control commands and records what was stored -- and the thread pool is replaced
by a direct call, because the real ``sync_to_async`` posts to the bot's event
loop. The filesystem is *not* faked: walking a download directory is a thing
this module actually does, and a temporary tree is the cheapest honest way to
check which files come out of it.
"""

from __future__ import annotations

import posixpath
import ssl
from ftplib import error_perm
from pathlib import Path

import pytest

import bot.helper.upload.ftp_uploader as fpu
from bot.core.config_manager import Config

DEFAULTS = {
    "FTP_HOST": "server.test",
    "FTP_PORT": 21,
    "FTP_USERNAME": "mirror",
    "FTP_PASSWORD": "secret",
    "FTP_PATH": "upload",
    "FTP_TLS": False,
}
"""Everything ``ftp_config_error`` insists on, plus a path to land in."""


class FakeListener:
    """The slice of a task the uploader reads and reports back to."""

    def __init__(self, mid=10032, name="Big.Buck.Bunny"):
        self.mid = mid
        self.name = name
        self.is_cancelled = False
        self.completed: tuple | None = None
        self.errors: list[str] = []

    async def on_upload_complete(self, link, files, folders, mime_type):
        self.completed = (link, files, folders, mime_type)

    async def on_upload_error(self, error, *args, **kwargs):
        self.errors.append(error)


class FakeFTP:
    """The FTP commands this uploader uses, over a set of existing folders."""

    def __init__(self):
        self.existing = {"/", "/home/user"}
        self.here = "/home/user"
        self.connected: tuple | None = None
        self.login_args: tuple | None = None
        self.stored: dict[str, bytes] = {}
        self.fail_on: set[str] = set()
        self.quit_called = False

    def connect(self, host, port, timeout=None):
        self.connected = (host, port)

    def login(self, user, password):
        self.login_args = (user, password)

    def pwd(self):
        return self.here

    def cwd(self, path):
        target = path if path.startswith("/") else posixpath.join(self.here, path)
        target = posixpath.normpath(target)
        if target not in self.existing:
            raise error_perm("550 no such directory")
        self.here = target

    def mkd(self, path):
        target = posixpath.normpath(posixpath.join(self.here, path))
        if target in self.existing:
            raise error_perm("550 already exists")
        self.existing.add(target)

    def storbinary(self, cmd, fp, blocksize=8192):
        name = cmd.removeprefix("STOR ")
        data = bytearray()
        while chunk := fp.read(blocksize):
            data.extend(chunk)
        if name in self.fail_on:
            raise error_perm("550 refused")
        self.stored[posixpath.join(self.here, name)] = bytes(data)

    def voidresp(self):
        return "226 Transfer complete"

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


class FakeTLS(FakeFTP):
    """The same server over explicit TLS: ``PROT P`` is the extra command."""

    instances: list[FakeTLS] = []

    def __init__(self):
        super().__init__()
        self.protected = False
        FakeTLS.instances.append(self)

    def prot_p(self):
        self.protected = True


class FakeTLSWithoutCloseNotify(FakeTLS):
    """Stores the file, then ends the data socket with a bare TCP close.

    That is what ``ftplib`` reports as ``ssl.SSLEOFError`` from the ``unwrap``
    it performs after the last block: the bytes are on the server and the
    ``226`` reply is still waiting on the control channel.
    """

    def __init__(self):
        super().__init__()
        self.drained = 0

    def storbinary(self, cmd, fp, blocksize=8192):
        super().storbinary(cmd, fp, blocksize)
        raise ssl.SSLEOFError("EOF occurred in violation of protocol")

    def voidresp(self):
        self.drained += 1
        return "226 Transfer complete"


@pytest.fixture
def server(monkeypatch):
    """Configure a working server and run the thread pool inline."""
    for name, value in DEFAULTS.items():
        monkeypatch.setattr(Config, name, value)

    async def immediate(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(fpu, "sync_to_async", immediate)
    fake = FakeFTP()
    monkeypatch.setattr(fpu, "FTP", lambda: fake)
    return fake


def tree(root: Path, *relative_paths: str) -> Path:
    """Create *relative_paths* under *root*, the last name being a real file."""
    for rel in relative_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
    return root


# ── one task, one folder ────────────────────────────────────────────


async def test_one_task_lands_in_one_folder_named_after_its_id(server, tmp_path):
    """The payload folder is peeled off, so 10032/ holds the album's files."""
    up_dir = tmp_path / "10032"
    tree(up_dir, "Big.Buck.Bunny/a.mkv", "Big.Buck.Bunny/b.mkv")
    listener = FakeListener(name="Big.Buck.Bunny")
    uploader = fpu.FTPUploader(listener, str(up_dir))

    await uploader.upload()

    assert set(server.stored) == {
        "/home/user/upload/10032/a.mkv",
        "/home/user/upload/10032/b.mkv",
    }
    assert listener.completed is not None
    link = listener.completed[0]
    assert link == "ftp://server.test/upload/10032/"
    assert listener.completed[1] == {link: "upload/10032/"}
    assert listener.completed[2] == 2
    assert listener.completed[3] == 0
    assert uploader.processed_bytes == sum(map(len, server.stored.values()))
    assert server.quit_called is True


async def test_sub_directories_keep_their_shape(server, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Big.Buck.Bunny/Season 1/ep1.mkv")
    listener = FakeListener(name="Big.Buck.Bunny")

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert set(server.stored) == {
        "/home/user/upload/10032/Season 1/ep1.mkv",
    }


async def test_a_bare_file_task_has_no_payload_folder(server, tmp_path):
    up_dir = tmp_path / "10032"
    up_dir.mkdir()
    (up_dir / "movie.mkv").write_bytes(b"movie")
    listener = FakeListener(name="movie.mkv")

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert set(server.stored) == {"/home/user/upload/10032/movie.mkv"}


async def test_the_login_uses_the_configured_credentials(server, tmp_path):
    tree(tmp_path, "10032/a.mkv")

    await fpu.FTPUploader(FakeListener(), str(tmp_path / "10032")).upload()

    assert server.connected == ("server.test", 21)
    assert server.login_args == ("mirror", "secret")


# ── what may go wrong ───────────────────────────────────────────────


async def test_a_missing_credential_fails_before_connecting(
    server, tmp_path, monkeypatch
):
    monkeypatch.setattr(Config, "FTP_PASSWORD", "")
    tree(tmp_path, "10032/a.mkv")
    listener = FakeListener()

    await fpu.FTPUploader(listener, str(tmp_path / "10032")).upload()

    assert listener.errors == ["FTP is not configured: set FTP_PASSWORD in config.py"]
    assert server.connected is None


async def test_one_refused_file_does_not_lose_the_others(server, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Big.Buck.Bunny/a.mkv", "Big.Buck.Bunny/b.mkv")
    server.fail_on = {"b.mkv"}
    listener = FakeListener(name="Big.Buck.Bunny")

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert set(server.stored) == {"/home/user/upload/10032/a.mkv"}
    assert listener.completed[2] == 2
    assert listener.completed[3] == 1


async def test_a_zero_byte_file_is_counted_and_never_stored(server, tmp_path):
    up_dir = tmp_path / "10032"
    payload = up_dir / "Big.Buck.Bunny"
    payload.mkdir(parents=True)
    (payload / "empty.txt").write_bytes(b"")
    (payload / "real.mkv").write_bytes(b"data")
    listener = FakeListener(name="Big.Buck.Bunny")

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert set(server.stored) == {"/home/user/upload/10032/real.mkv"}
    assert listener.completed[2] == 2
    assert listener.completed[3] == 1


async def test_a_task_with_no_files_to_store_is_reported(server, tmp_path):
    up_dir = tmp_path / "10032"
    up_dir.mkdir()
    listener = FakeListener()

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert listener.completed is None
    assert listener.errors == [fpu.NO_FILES_ERROR]


async def test_every_file_refused_is_an_error_not_an_empty_success(server, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Big.Buck.Bunny/a.mkv")
    server.fail_on = {"a.mkv"}
    listener = FakeListener(name="Big.Buck.Bunny")

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert listener.completed is None
    assert listener.errors and "Corrupted" in listener.errors[0]


async def test_a_cancelled_task_stores_nothing_and_reports_nothing(server, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Big.Buck.Bunny/a.mkv")
    listener = FakeListener(name="Big.Buck.Bunny")
    listener.is_cancelled = True

    await fpu.FTPUploader(listener, str(up_dir)).upload()

    assert server.stored == {}
    assert listener.completed is None
    assert listener.errors == []


async def test_explicit_tls_protects_the_transfer(server, tmp_path, monkeypatch):
    FakeTLS.instances.clear()
    monkeypatch.setattr(fpu, "FTP_TLS", FakeTLS)
    monkeypatch.setattr(Config, "FTP_TLS", True)
    tree(tmp_path, "10032/a.mkv")

    await fpu.FTPUploader(FakeListener(), str(tmp_path / "10032")).upload()

    assert len(FakeTLS.instances) == 1
    assert FakeTLS.instances[0].protected is True
    # The plain server from the fixture was never opened.
    assert server.login_args is None


async def test_a_missing_tls_close_notify_after_stor_is_not_a_failure(
    server, tmp_path, monkeypatch
):
    FakeTLS.instances.clear()
    monkeypatch.setattr(fpu, "FTP_TLS", FakeTLSWithoutCloseNotify)
    monkeypatch.setattr(Config, "FTP_TLS", True)
    tree(tmp_path, "10032/a.mkv")
    listener = FakeListener()

    await fpu.FTPUploader(listener, str(tmp_path / "10032")).upload()

    assert listener.completed is not None
    assert listener.completed[2] == 1
    assert listener.completed[3] == 0
    # The STOR reply the unwrap never read is drained, so the next command
    # does not receive it as its own answer.
    assert FakeTLS.instances[0].drained == 1
