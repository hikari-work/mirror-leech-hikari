"""Sequential PikPak share downloader.

The gateway's ``/api/v1/pikpak/download`` purges the account's temp folder before
every restore, so one result is valid only until the next call (the client module
documents the exact scope: global to the gateway's PikPak account, not per share
or per user). This module therefore:

* holds ``PIKPAK_DOWNLOAD_LOCK`` across the whole "call the gateway, stream the
  CDN link to disk, hand it to the next stage" sequence -- one file at a time,
  never ``gather`` over download calls;
* re-lists the share once (``nocache=1``) and retries the single file when a
  result is reported stale/expired, then reports the failure;
* names every local file from the gateway's response ``filename`` (sanitised),
  never from the share listing's display name;
* counts progress in bytes and sends "File i/n" to the user before each file.

Per-job state is the task itself: the original share link lives in
``listener.link["pikpak"]`` and the listing's file ids are held by this helper
for the duration of the task. That is the bot's existing per-task persistence;
a restart re-lists the share on the next attempt.
"""

from __future__ import annotations

from asyncio import Lock
from html import escape
from os import path as ospath

from aiofiles.os import makedirs, remove
from aiofiles.os import path as aiopath

from ... import LOGGER
from ..net.pikpak_client import (
    PikPakClient,
    PikPakEntry,
    PikPakError,
    sanitize_filename,
)
from ..telegram.message_utils import send_message
from ..util.resolve_gate import resolve_gate
from .multi_video_download import MultiVideoDownloadHelper

# One lock for every PikPak download in this process. The gateway purges its one
# account's temp folder on each call, so a second call anywhere invalidates the
# first result; a per-share lock would not. See the client module docstring for
# the cross-process gap the gateway itself would have to close.
PIKPAK_DOWNLOAD_LOCK = Lock()

_STALE_ATTEMPTS = 2


class PikPakDownloadHelper(MultiVideoDownloadHelper):
    """Download one PikPak share strictly in sequence, then hand off."""

    def __init__(
        self, listener, client: PikPakClient | None = None
    ) -> None:
        super().__init__(listener)
        self._client = client or PikPakClient()
        self._owns_client = client is None
        self._single_file = False

    def _make_status(self):
        from ..progress.pikpak_status import PikPakStatus

        return PikPakStatus(self._listener, self, self._gid)

    def _count_bytes(self, count: int) -> None:
        """Progress callback for one CDN chunk."""
        self._current_downloaded += count

    async def _notify_file(self, index: int, total: int, name: str) -> None:
        """Tell the user which file is next; the listing name is display-only."""
        label = escape(name or "file")
        LOGGER.info(f"PikPak: file {index}/{total} {name or ''}")
        if self._listener.message is not None:
            await send_message(
                self._listener.message, f"PikPak: file {index}/{total} — {label}"
            )

    async def _fetch_file(
        self,
        share_url: str,
        entry: PikPakEntry,
        base: str,
        taken: set[str],
        stream,
    ) -> tuple[str | None, str | None]:
        """Fetch one entry. The caller holds ``PIKPAK_DOWNLOAD_LOCK``.

        Returns ``(local_name, failure)``. A stale result is re-listed and tried
        exactly once; a second stale answer, or any other error, is a failure.
        The CDN stream is fully written (and handed to *stream*) before this
        returns, so the next gateway call cannot invalidate it.
        """
        current = entry
        for attempt in range(1, _STALE_ATTEMPTS + 1):
            dest: str | None = None
            self._current_downloaded = 0
            try:
                info = await self._client.download(share_url, current.id)
                local_name = _unique_name(sanitize_filename(info.filename), taken)
                dest = ospath.join(base, local_name)
                await self._client.download_to(
                    info.url,
                    dest,
                    on_bytes=self._count_bytes,
                    is_cancelled=lambda: self._listener.is_cancelled,
                )
                if self._listener.is_cancelled:
                    await _discard(dest)
                    return None, "cancelled"
                self._processed += self._current_downloaded
                self._current_downloaded = 0
                taken.add(local_name.lower())
                if stream is not None:
                    await stream.submit(dest)
                return local_name, None
            except PikPakError as exc:
                if dest is not None:
                    await _discard(dest)
                self._current_downloaded = 0
                if self._listener.is_cancelled:
                    return None, "cancelled"
                if not exc.stale or attempt == _STALE_ATTEMPTS:
                    return None, str(exc)
                LOGGER.info(
                    f"PikPak: file {current.id} is stale ({exc}); "
                    "re-listing the share"
                )
                try:
                    listing = await self._client.share(share_url, fresh=True)
                except PikPakError as list_exc:
                    return None, str(list_exc)
                refreshed = next(
                    (
                        item
                        for item in listing
                        if item.id == current.id and not item.is_folder
                    ),
                    None,
                )
                if refreshed is None:
                    return None, "this file is no longer in the share"
                current = refreshed
        return None, "download failed"

    async def _download_files(
        self,
        share_url: str,
        files: list[PikPakEntry],
        base: str,
        stream=None,
    ) -> list[str]:
        """Download *files* in listing order and return the failures."""
        failures: list[str] = []
        taken: set[str] = set()
        self._total_count = len(files)
        for index, entry in enumerate(files, 1):
            if self._listener.is_cancelled:
                break
            await self._notify_file(index, len(files), entry.name)
            async with PIKPAK_DOWNLOAD_LOCK:
                if self._listener.is_cancelled:
                    break
                local_name, failure = await self._fetch_file(
                    share_url, entry, base, taken, stream
                )
            if failure:
                label = entry.name or entry.id
                failures.append(f"{label}: {failure}")
                LOGGER.error(
                    f"PikPak: file {index}/{len(files)} failed: "
                    f"{label} ({failure})"
                )
                if not self._listener.is_cancelled:
                    await self._report_failure(index, len(files), failure)
                continue
            self._done_count += 1
            if self._single_file and local_name:
                self._listener.name = local_name
        return failures

    async def _report_failure(self, index: int, total: int, failure: str) -> None:
        """One user-facing line per failed file; the task decides the rest."""
        if self._listener.message is None:
            return
        await send_message(
            self._listener.message,
            f"PikPak: file {index}/{total} failed — {escape(failure)}",
        )

    async def _list_files(
        self, share_url: str, share_id: str
    ) -> list[PikPakEntry] | None:
        """The share's files, or ``None`` after reporting a listing failure."""
        try:
            async with resolve_gate():
                listing = await self._client.share(share_url)
        except PikPakError as exc:
            LOGGER.error(f"PikPak: cannot list {share_id}: {exc}")
            await self._listener.on_download_error(str(exc))
            return None
        files = [entry for entry in listing if entry.id and not entry.is_folder]
        if not files:
            await self._listener.on_download_error(
                "PikPak share has no downloadable files."
            )
            return None
        return files

    async def _finish(
        self, files: list[PikPakEntry], failures: list[str], stream
    ) -> None:
        """Report the outcome and start the bot's existing upload step."""
        if self._listener.is_cancelled:
            return
        if len(failures) == len(files):
            await self._listener.on_download_error(
                f"PikPak: no file could be downloaded ({failures[0]})"
            )
            return
        if failures:
            LOGGER.error(f"PikPak: {len(failures)}/{len(files)} file(s) failed")
            self._listener.size = self._processed
        if stream is not None:
            await stream.finalize()
            return
        await self._listener.on_download_complete()

    async def add_download(self, path: str) -> None:
        """List the share, download every file in order, then upload."""
        details = self._listener.link["pikpak"]
        share_url = details["url"]
        try:
            files = await self._list_files(share_url, details["share_id"])
            if files is None:
                return
            self._single_file = len(files) == 1
            if not self._listener.name:
                self._listener.name = f"PikPak-{details['share_id']}"
            self._listener.size = sum(entry.size for entry in files)
            if not await self._start():
                return
            LOGGER.info(
                f"Download from PikPak: {self._listener.name} "
                f"({len(files)} file{'s' if len(files) != 1 else ''})"
            )
            # A single file goes straight into the task directory and takes the
            # gateway's name; several go into one folder, since a bulk merges
            # task directories and loose files would lose their share.
            base = (
                path
                if self._single_file
                else ospath.join(path, self._listener.name)
            )
            await makedirs(base, exist_ok=True)
            stream = None
            if self._listener.stream_upload:
                # Imported here: this is the upload side of this package.
                from ..upload.stream_uploader import StreamUploader

                stream = StreamUploader(self._listener, base)
                if not await stream.start():
                    return
            try:
                failures = await self._download_files(
                    share_url, files, base, stream
                )
            finally:
                if stream is not None:
                    await stream.drain()
            await self._finish(files, failures, stream)
        finally:
            if self._owns_client and self._client is not None:
                await self._client.aclose()


def _unique_name(name: str, taken: set[str]) -> str:
    """*name*, or one that has not been used in this listing yet."""
    if name.lower() not in taken:
        return name
    stem, extension = ospath.splitext(name)
    index = 2
    while f"{stem} ({index}){extension}".lower() in taken:
        index += 1
    return f"{stem} ({index}){extension}"


async def _discard(path: str) -> None:
    """Remove a partial file; a failed task must not leave one for upload."""
    try:
        if await aiopath.exists(path):
            await remove(path)
    except OSError as exc:
        LOGGER.warning(f"PikPak: cannot remove partial file {path}: {exc}")


async def add_pikpak_download(listener, path: str) -> None:
    """Entry point used by ``leech._dispatch_download``.

    ``add_download`` reports the failures it knows about; this guard keeps an
    unexpected one (a CDN read, the stream uploader) from leaving the task
    without an outcome.
    """
    try:
        await PikPakDownloadHelper(listener).add_download(path)
    except Exception as exc:
        LOGGER.exception("PikPak: task failed unexpectedly")
        await listener.on_download_error(f"PikPak: {exc}")

