"""Sequential PikPak share downloader.

The gateway's ``/api/v1/pikpak/download`` purges the account's temp folder before
every restore, so one result is valid only until the next call (the client module
documents the exact scope: global to the gateway's PikPak account, not per share
or per user). This module therefore:

* holds ``PIKPAK_DOWNLOAD_LOCK`` across the whole "call the gateway, fetch the
  CDN link (aria2 first, one httpx stream as fallback), hand it to the next
  stage" sequence -- one file at a time, never ``gather`` over download calls;
* re-lists the share once (``nocache=1``) and retries the single file when a
  result is reported stale/expired, then reports the failure;
* names every local file from the gateway's response ``filename`` (sanitised),
  never from the share listing's display name;
* counts progress in bytes for the task status line; per-file notices are
  log-only, like the other engines, so a 30-file share sends no chat spam.

Per-job state is the task itself: the original share link lives in
``listener.link["pikpak"]`` and the listing's file ids are held by this helper
for the duration of the task. That is the bot's existing per-task persistence;
a restart re-lists the share on the next attempt.
"""

from __future__ import annotations

from asyncio import Lock, sleep
from os import path as ospath

from aiofiles.os import makedirs, remove
from aiofiles.os import path as aiopath

from ... import LOGGER
from ..net.pikpak_client import (
    PikPakClient,
    PikPakDownload,
    PikPakEntry,
    PikPakError,
    sanitize_filename,
)
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

    async def _download_cdn(self, info: PikPakDownload, dest: str) -> None:
        """Fill *dest* from the gateway's CDN link.

        aria2 is preferred because it is the same engine, options and retry
        behaviour as ``/leech <cdn-url>``: the CDN answers this client's own
        concurrent Range requests with HTTP 503, and one httpx stream is the
        slow fallback when aria2 is not running.
        """
        if await self._download_via_aria2(info.url, dest):
            return
        self._current_downloaded = 0
        await self._client.download_to(
            info.url,
            dest,
            on_bytes=self._count_bytes,
            is_cancelled=lambda: self._listener.is_cancelled,
        )

    async def _download_via_aria2(self, url: str, dest: str) -> bool:
        """Start one aria2 download and wait it out; False means "use httpx".

        The options mirror ``aria-nox.sh``: ten connections, ten-megabyte
        pieces. They are set per download so the fallback and the rest of the
        bot do not inherit them.
        """
        from ...core.torrent_manager import TorrentManager

        aria2 = TorrentManager.aria2
        if aria2 is None:
            return False
        options = {
            "dir": ospath.dirname(dest),
            "out": ospath.basename(dest),
            "split": "10",
            "max-connection-per-server": "10",
            "min-split-size": "10M",
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
        }
        try:
            gid = await aria2.addUri(
                uris=[url], options=options, position=0
            )
            status = await TorrentManager.aria2_status(gid)
        except Exception as exc:
            LOGGER.info(f"PikPak: aria2 did not start ({exc!r}); httpx stream")
            return False
        return await self._wait_for_aria2(gid, status, dest)

    async def _wait_for_aria2(
        self, gid: str, status: dict, dest: str
    ) -> bool:
        """Poll one aria2 download to completion; False means "use httpx"."""
        from ...core.torrent_manager import TorrentManager

        while True:
            if self._listener.is_cancelled:
                await TorrentManager.aria2_remove(status)
                return True
            if error := status.get("errorMessage"):
                LOGGER.warning(f"PikPak: aria2 failed for {dest}: {error}")
                await _drop_aria2(status, dest)
                return False
            if status.get("status") == "complete":
                self._current_downloaded = _completed_length(status)
                await TorrentManager.aria2_remove(status)
                return True
            self._current_downloaded = _completed_length(status)
            await sleep(1)
            try:
                status = await TorrentManager.aria2_status(gid)
            except Exception as exc:
                LOGGER.warning(f"PikPak: aria2 status failed: {exc!r}")
                await _drop_aria2(status, dest)
                return False

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
                await self._download_cdn(info, dest)
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
            LOGGER.info(
                f"PikPak: file {index}/{len(files)} {entry.name or entry.id}"
            )
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
                continue
            self._done_count += 1
            if self._single_file and local_name:
                self._listener.name = local_name
        return failures

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


def _completed_length(status: dict) -> int:
    """aria2 reports lengths as strings."""
    try:
        return max(0, int(status.get("completedLength") or 0))
    except (TypeError, ValueError):
        return 0


async def _drop_aria2(status: dict, dest: str) -> None:
    """Stop a failed aria2 job and remove what it left behind."""
    from ...core.torrent_manager import TorrentManager

    try:
        await TorrentManager.aria2_remove(status)
    except Exception as exc:
        LOGGER.warning(f"PikPak: cannot remove aria2 job: {exc!r}")
    await _discard(dest)
    await _discard(f"{dest}.aria2")


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

