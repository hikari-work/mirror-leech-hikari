"""Upload a finished task's files to an FTP server.

The second uploader that does not talk to telegram, next to the bucket one, and
keyed the same way: one folder per task, named after the task id, under the
configured ``FTP_PATH``. The completion message carries a single ``ftp://`` link
to that folder rather than a telegram message per file.

``ftplib`` is blocking, so the whole session -- login, the directory walk, every
``STOR`` -- runs in one thread-pool job. The status line reads the byte counter
this module keeps while it runs, which is the same contract ``S3Uploader`` has:
from the outside only ``upload``, ``processed_bytes``, ``speed`` and
``cancel_task`` are used.
"""

from __future__ import annotations

import ssl
from ftplib import FTP, FTP_TLS, error_perm
from os import path as ospath
from os import walk
from threading import Lock
from time import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from natsort import natsorted

from ... import LOGGER
from ...core.config_manager import Config
from ..util.bot_utils import sync_to_async

if TYPE_CHECKING:
    from ..listeners.task_listener import TaskListener


NO_FILES_ERROR = (
    "No files to upload. In case you have filled EXCLUDED/INCLUDED EXTENSIONS, "
    "then check if all files have those extensions or not."
)

_SKIPPED_DIRS = ("yt-dlp-thumb", "_mltbss")
"""Directories that exist for telegram rather than for the server. The bucket
uploader skips the same two, for the same reason."""

_BLOCKSIZE = 256 * 1024
"""Bytes read and sent per ``STOR`` write, and the granularity of the progress
count -- fine enough for a live status line, large enough to keep Python out of
the socket's way."""

_CONNECT_TIMEOUT = 60
"""Seconds ftplib may spend on the control connection before giving up, so a
server that accepts the socket and then says nothing costs a task a minute
instead of hanging it for the life of the process."""


def ftp_config_error() -> str:
    """Why a mirror task cannot run, or ``""`` when it can.

    The three settings a task needs for its files to be stored, plus the path
    they land in. Checked before the download rather than after it: failing an
    hour of transfer because a password was unset is the failure this exists to
    prevent.
    """
    missing = [
        name
        for name in ("FTP_HOST", "FTP_USERNAME", "FTP_PASSWORD")
        if not getattr(Config, name)
    ]
    if not missing:
        return ""
    return f"FTP is not configured: set {', '.join(missing)} in config.py"


def remote_dir(listener: TaskListener) -> str:
    """The folder every file of *listener* goes into.

    ``FTP_PATH`` below the login directory, the task id under it: ``"10032"``,
    or ``"upload/10032"`` when ``FTP_PATH`` is ``"upload"``. Slashes are
    stripped here as well as by the config loader, because a ``/bsetting`` write
    goes through ``Config.set``, which never reaches the normalising path.
    """
    base = str(Config.FTP_PATH or "").strip("/")
    return f"{base}/{listener.mid}" if base else str(listener.mid)


def ftp_port() -> int:
    return int(Config.FTP_PORT or 21)


def browse_url(listener: TaskListener) -> str:
    """The one link a completed task reports: its folder on the server.

    ``FTP_HOST`` holds a hostname, so the port is appended only when it is not
    the protocol's own -- ``ftp://host:2121/…`` reads as a different address,
    while ``ftp://host:21/…`` is noise. The path keeps its separators:
    ``safe="/"`` is what stops ``10032/`` from becoming ``10032%2F``.
    """
    host = str(Config.FTP_HOST or "").strip()
    port = ftp_port()
    authority = host if port == 21 else f"{host}:{port}"
    folder = quote(remote_dir(listener) + "/", safe="/")
    return f"ftp://{authority}/{folder}"


class _UploadCancelled(Exception):
    """Raised from the file reader to stop a ``STOR`` that is under way."""


class _Reader:
    """The file object ``storbinary`` reads, counting bytes as it goes.

    ``/cancel`` cannot interrupt a socket write that is already in flight, but
    it can stop the next read, which is what this does -- the same job boto3's
    callback does for a bucket upload.
    """

    def __init__(self, uploader: FTPUploader, file_obj: Any) -> None:
        self._uploader = uploader
        self._file = file_obj

    def read(self, size: int = -1) -> bytes:
        if self._uploader.is_cancelled:
            raise _UploadCancelled
        data = self._file.read(size)
        self._uploader.note_progress(len(data))
        return data


class FTPUploader:
    """Puts one task's files on the FTP server, in one folder named after it."""

    def __init__(self, listener: TaskListener, path: str) -> None:
        self._listener = listener
        self._path = path
        # Where the remote keys are relative to, decided in ``upload`` -- the
        # payload directory when there is one, the task directory otherwise.
        self._walk_root = path
        self._remote = remote_dir(listener)
        self._ftp: FTP | None = None
        self._home = "/"
        self._lock = Lock()
        self._processed_bytes = 0
        self._start_time = time()
        self._total_files = 0
        self._corrupted = 0
        self._error = ""

    @property
    def is_cancelled(self) -> bool:
        return self._listener.is_cancelled

    def note_progress(self, bytes_amount: int) -> None:
        with self._lock:
            self._processed_bytes += bytes_amount

    @property
    def processed_bytes(self) -> int:
        return self._processed_bytes

    @property
    def speed(self) -> float:
        """Bytes a second since the upload started, for the status line."""
        elapsed = time() - self._start_time
        return self._processed_bytes / elapsed if elapsed > 0 else 0

    async def upload(self) -> None:
        """Store every file of the task, then report where they went."""
        error = ftp_config_error()
        if error:
            await self._listener.on_upload_error(error)
            return
        try:
            self._walk_root = self._root()
            LOGGER.info(
                f"FTP upload: {self._listener.name} -> {self._remote}/"
            )
            await sync_to_async(self._run)
        except _UploadCancelled:
            return
        except Exception as err:
            LOGGER.error(f"FTP upload failed: {err}")
            await self._listener.on_upload_error(f"FTP upload failed: {err}")
            return
        await self._finish()

    def _run(self) -> None:
        """The blocking half: connect, walk the task folder, store every file."""
        ftp = self._connect()
        try:
            self._home = ftp.pwd()
            self._ensure_dir(self._remote)
            walk_result = list(walk(self._walk_root))
            for dirpath, _, files in natsorted(walk_result):
                if self.is_cancelled:
                    return
                if ospath.basename(dirpath) in _SKIPPED_DIRS:
                    continue
                rel = ospath.relpath(dirpath, self._walk_root)
                target = (
                    self._remote
                    if rel == "."
                    else f"{self._remote}/{rel.replace(ospath.sep, '/')}"
                )
                self._ensure_dir(target)
                for file_ in natsorted(files):
                    if self.is_cancelled:
                        return
                    self._upload_one(ospath.join(dirpath, file_), file_)
        finally:
            self._disconnect(ftp)

    def _connect(self) -> FTP:
        """An FTP session logged in with the configured credentials."""
        ftp: FTP = FTP_TLS() if Config.FTP_TLS else FTP()
        ftp.connect(Config.FTP_HOST, ftp_port(), timeout=_CONNECT_TIMEOUT)
        ftp.login(Config.FTP_USERNAME, Config.FTP_PASSWORD)
        if isinstance(ftp, FTP_TLS):
            # Explicit TLS: the control channel is up, and ``PROT P`` is what
            # puts the transfers on it as well.
            ftp.prot_p()
        self._ftp = ftp
        return ftp

    def _disconnect(self, ftp: FTP) -> None:
        """Close the session, tolerating a server that has already hung up."""
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def _ensure_dir(self, target: str) -> None:
        """Create the parts of *target* that are missing, then enter it.

        Every call starts from the login directory (or the server root, for an
        absolute path): ``_run`` asks for sibling folders as the walk proceeds,
        and a session that stayed where the previous file left it would build
        ``upload/upload/10032`` instead.
        """
        ftp = self._ftp
        assert ftp is not None
        ftp.cwd("/" if target.startswith("/") else self._home)
        for part in target.split("/"):
            if not part:
                continue
            try:
                ftp.cwd(part)
            except error_perm:
                ftp.mkd(part)
                ftp.cwd(part)

    def _root(self) -> str:
        """The directory the remote paths are relative to.

        ``up_dir`` holds the payload under a name of its own -- the download's
        -- plus, sometimes, a thumbnail scratch folder. Walking from there would
        put every file under ``10032/Big.Buck.Bunny/…``, so the task's folder
        would no longer be the one the completion message links to. The payload
        directory itself is the root when it is one; a bare file has no folder
        of its own to peel off.
        """
        payload = ospath.join(self._path, self._listener.name)
        return payload if ospath.isdir(payload) else self._path

    def _upload_one(self, file_path: str, name: str) -> None:
        """Store one file, counting a failure instead of dying on it.

        The same contract the other uploaders keep: one unreadable or refused
        file must not lose the other nine hundred, and a zero-byte file is
        counted and never sent -- an object nobody can fetch has no value.
        """
        size = ospath.getsize(file_path)
        self._total_files += 1
        if size == 0:
            LOGGER.error(f"{file_path} size is zero, nothing to store")
            self._corrupted += 1
            return
        if self.is_cancelled:
            return
        ftp = self._ftp
        assert ftp is not None
        self._error = ""
        try:
            with open(file_path, "rb") as file_obj:
                try:
                    ftp.storbinary(
                        f"STOR {name}",
                        _Reader(self, file_obj),
                        blocksize=_BLOCKSIZE,
                    )
                except ssl.SSLError:
                    # ``storbinary`` unwraps the data socket after the last
                    # block, and a server that ends the transfer with a bare
                    # TCP close makes that unwrap raise "EOF occurred in
                    # violation of protocol" -- after the bytes were stored.
                    # The reply to STOR is still unread, so drain it here;
                    # a genuine mid-transfer failure answers 4xx/5xx and the
                    # outer handler counts it.
                    ftp.voidresp()
        except _UploadCancelled:
            # the listener knows; the caller stops walking the rest
            raise
        except Exception as err:
            # Same contract as the bucket uploader: a refused or unreadable file
            # is counted, and a dead connection fails the rest the same way --
            # the outcome is reported once, from ``_finish``.
            LOGGER.error(f"FTP upload failed for {name}: {err}")
            self._error = str(err)
            self._corrupted += 1

    async def _finish(self) -> None:
        """Report the outcome, or the folder link when there is one."""
        if self.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(NO_FILES_ERROR)
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        LOGGER.info(f"FTP upload completed: {self._listener.name}")
        link = browse_url(self._listener)
        await self._listener.on_upload_complete(
            link,
            {link: f"{self._remote}/"},
            self._total_files,
            self._corrupted,
        )

    async def cancel_task(self) -> None:
        """Stop the transfer, the way every other status object can be asked to.

        The flag is what the reader tests to abort a ``STOR`` already in flight
        -- a future on the thread pool cannot be called back once it is running.
        """
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")
