"""PikPak share client for the piyann API gateway.

Verified against the gateway source in ``/home/stefani/api-gate``
(``src/pikpak/{handlers,models,scraper}.rs``):

* Both routes are **GET** under ``/api/v1`` and take the gateway bearer token
  (``Authorization: Bearer <GATEWAY_TOKEN>``). A missing/bad token answers 401
  ``{"success": false, "error": ...}``; 503 when ``AUTH_BEARER_TOKENS`` is unset.
* ``GET /api/v1/pikpak/share?url=<share link>`` answers
  ``{success, share_id, total_files, files[{id, name, kind, is_folder, size,
  mime_type, parent_id}], error, timestamp, duration_ms}``. Read-only and
  recursive, with no file-count or folder-depth cap.
* ``GET /api/v1/pikpak/download?url=<same share link>&file_id=<id>`` answers
  ``{success, download_url, file_id, filename, file_size, expires_at, purged,
  error, timestamp, duration_ms}``. ``file_id`` is the id the file has in the
  drive after the restore, not the share id passed in.
* ``filename`` is a **response** field, not a request parameter. It is the name
  the restore actually created and is the only trusted name; the listing's
  ``name`` is display-only.
* A pass code travels inside ``url`` as ``?pwd=<code>``. There is no separate
  password parameter. A protected share answers HTTP 500 with an error such as
  ``pikpak share V... is PASS_CODE_NEEDED ...``.
* Errors: 400 for a malformed/missing ``url`` or ``file_id``; 500 for every
  upstream failure (expired/removed share, pass code needed, restore or quota
  failure), with the reason only in the body's ``error`` string -- there is no
  machine-readable error code; 429 for the per-token fixed window (default 100
  requests / 60 s, with ``Retry-After`` and ``retry_after``); 503 when auth is
  unconfigured.
* Timeouts: the gateway's upstream call is 30 s (``SCRAPER_TIMEOUT``) and a
  restore may take 90 s, so this client allows 180 s of read silence.
* The share route is Redis-cached for 1 h (detail class); ``nocache=1`` skips
  it, which is what the stale-result retry uses.
* Invalidation is **global**, not per share or per user. Every download call
  purges the one gateway PikPak account's ``api-downloads`` folder (or the whole
  drive when ``PIKPAK_PURGE_ALL=1``) before restoring the next file. One result
  is valid only until the next download call anywhere against that account.
  The bot serialises every download behind one process-wide lock; the gateway
  has no lock itself, so a second bot process or any other client sharing the
  account can still purge a result under us. That is reported, not worked
  around: the correct fix is a lock on the gateway side.
"""

from __future__ import annotations

from asyncio import sleep
from collections.abc import Callable
from dataclasses import dataclass
from logging import getLogger
from os import path as ospath
from re import compile as re_compile
from time import time
from typing import Any
from unicodedata import category, normalize
from urllib.parse import urlparse

import httpx
from aiofiles import open as aiopen

from .gateway import gateway_headers, gateway_url

LOGGER = getLogger(__name__)

_CHUNK = 1024 * 1024
_MAX_FILENAME_BYTES = 200
_SHARE_HOSTS = frozenset({"mypikpak.com", "www.mypikpak.com"})
_SHARE_SEGMENT = re_compile(r"^[A-Za-z0-9_-]+$")
_TIMEOUT = httpx.Timeout(300.0, connect=10.0, read=180.0, write=60.0, pool=10.0)
# A gateway 5xx/429 is transient; share is read-only, so retrying it is safe.
_SHARE_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Restore calls are expensive and write to the account's drive, so only a
# gateway answer that certainly did not finish is retried.
_DOWNLOAD_RETRY_STATUSES = frozenset({429, 502, 504})
_SHARE_ATTEMPTS = 3
_MAX_RETRY_DELAY = 30.0
# The gateway only copies text from the upstream error, so matching is textual.
_STALE_HINTS = (
    "file_not_found",
    "not found",
    "not_found",
    "expired",
    "gone",
    "removed",
    "cancelled",
    "canceled",
)
_PASSWORD_HINTS = ("pass_code", "passcode", "password", "pwd")


class PikPakError(Exception):
    """A PikPak gateway/CDN failure whose message is fit for the user."""

    def __init__(
        self,
        message: str,
        *,
        stale: bool = False,
        status: int = 0,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.stale = stale
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class PikPakEntry:
    """One entry of a share listing."""

    id: str
    name: str
    size: int
    is_folder: bool


@dataclass(frozen=True)
class PikPakDownload:
    """One resolved download: the CDN URL and the gateway's trusted name."""

    url: str
    file_id: str
    filename: str
    size: int
    expires_at: int


def is_pikpak_link(url: str) -> bool:
    """Whether *url* points at a mypikpak.com share, valid or not.

    Deliberately host-only: a mistyped path must reach :func:`parse_share_link`
    and produce "invalid link", not fall through to the generic resolver.
    """
    try:
        return (urlparse(url).hostname or "").lower() in _SHARE_HOSTS
    except ValueError:
        return False


def parse_share_link(link: str) -> str:
    """Validate a share link and return it stripped, query (``?pwd=``) intact."""
    text = (link or "").strip()
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise PikPakError(_INVALID_LINK) from exc
    if parsed.scheme not in ("http", "https") or host not in _SHARE_HOSTS:
        raise PikPakError(_INVALID_LINK)
    segments = [part for part in parsed.path.split("/") if part]
    if (
        len(segments) != 2
        or segments[0] != "s"
        or not _SHARE_SEGMENT.fullmatch(segments[1])
    ):
        raise PikPakError(_INVALID_LINK)
    return text


_INVALID_LINK = (
    "Invalid PikPak link. Send a share link like "
    "https://mypikpak.com/s/VP45AOkZd6hLOKNtqrKemsUKo2"
)


def share_id_of(link: str) -> str:
    """The ``<share_id>`` segment of an already validated share link."""
    return [part for part in urlparse(link).path.split("/") if part][-1]


def sanitize_filename(name: str, fallback: str = "pikpak-file") -> str:
    """Make a gateway filename safe for the local filesystem and Telegram.

    Keeps the name exactly as the gateway gave it otherwise: NFC-normalised,
    ``@`` and non-ASCII preserved (no transliteration), path separators and
    ``..`` sequences neutralised, control characters dropped, and the UTF-8
    length capped while keeping the extension.
    """
    cleaned = normalize("NFC", name or "")
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = "".join(
        char for char in cleaned if category(char) not in ("Cc", "Cs")
    )
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return fallback
    stem, extension = ospath.splitext(cleaned)
    if len(cleaned.encode("utf-8")) <= _MAX_FILENAME_BYTES:
        return cleaned
    budget = _MAX_FILENAME_BYTES - len(extension.encode("utf-8"))
    if budget <= 0:
        return fallback
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
    return f"{stem}{extension}" if stem else fallback


class PikPakClient:
    """Async PikPak gateway client: ``share()`` and ``download()``.

    One instance per task is fine; it owns one connection pool. Pass ``http``
    to reuse an existing :class:`httpx.AsyncClient` (tests do).
    """

    def __init__(self, *, http: httpx.AsyncClient | None = None) -> None:
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True
        )

    async def __aenter__(self) -> PikPakClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the pool this client created; an injected client stays open."""
        if self._owns_http:
            await self._http.aclose()

    async def share(self, link: str, *, fresh: bool = False) -> list[PikPakEntry]:
        """List every entry of a share, folders included.

        *fresh* passes ``nocache=1`` so a retry does not read the gateway's
        one-hour cached listing.
        """
        link = parse_share_link(link)
        params = {"url": link}
        if fresh:
            params["nocache"] = "1"
        data = await self._share_json(params)
        files = data.get("files")
        if not isinstance(files, list):
            files = []
        entries = []
        for item in files:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            entries.append(
                PikPakEntry(
                    id=str(item["id"]),
                    name=str(item.get("name") or ""),
                    size=max(0, size),
                    is_folder=bool(item.get("is_folder"))
                    or item.get("kind") == "drive#folder",
                )
            )
        LOGGER.info(
            "PikPak share %s: %d entries", share_id_of(link), len(entries)
        )
        return entries

    async def download(self, link: str, file_id: str) -> PikPakDownload:
        """Restore one share entry and return its CDN link and trusted name.

        A 429/502/504 is retried once: nothing was downloaded from the first
        attempt, and the next call purges whatever it may have restored. A 500
        is final -- it carries the upstream reason (expired share, quota, ...).
        """
        link = parse_share_link(link)
        if not file_id:
            raise PikPakError("PikPak returned a file without an id.")
        data = await self._download_json(
            {"url": link, "file_id": file_id}
        )
        url = str(data.get("download_url") or "")
        filename = str(data.get("filename") or "")
        if not url:
            raise PikPakError(
                "PikPak did not return a download link for this file.", stale=True
            )
        if not filename:
            raise PikPakError("PikPak did not return a filename for this file.")
        try:
            expires_at = int(data.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= time():
            raise PikPakError(
                "PikPak returned an already expired download link.", stale=True
            )
        try:
            size = int(data.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        LOGGER.info(
            "PikPak download %s/%s: %s (%d bytes)",
            share_id_of(link),
            file_id,
            filename,
            size,
        )
        return PikPakDownload(
            url=url,
            file_id=str(data.get("file_id") or file_id),
            filename=filename,
            size=max(0, size),
            expires_at=expires_at,
        )

    async def download_to(
        self,
        url: str,
        dest: str,
        *,
        on_bytes: Callable[[int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> int:
        """Stream *url* into *dest* one chunk at a time and return the byte count.

        One connection on purpose: the PikPak CDN answers concurrent Range
        requests from this client with HTTP 503, while a single stream (or
        aria2, which the downloader prefers) succeeds. See
        ``pikpak_download._download_via_aria2`` for the fast path.

        A 404/410 is a stale result the caller may retry; anything else is a
        reportable failure. Cancellation stops the stream and leaves the partial
        file for the caller to remove.
        """
        written = 0
        try:
            async with self._http.stream("GET", url) as response:
                _check_cdn_status(response, url)
                async with aiopen(dest, "wb") as handle:
                    async for chunk in response.aiter_bytes(_CHUNK):
                        if is_cancelled is not None and is_cancelled():
                            break
                        await handle.write(chunk)
                        written += len(chunk)
                        if on_bytes is not None:
                            on_bytes(len(chunk))
        except httpx.TimeoutException as exc:
            raise PikPakError("PikPak download timed out. Try again.") from exc
        except httpx.HTTPError as exc:
            LOGGER.warning("PikPak CDN request failed: %r", exc)
            raise PikPakError("PikPak download failed: network error.") from exc
        return written

    async def _download_json(self, params: dict[str, str]) -> dict[str, Any]:
        """One restore call, retried once when the gateway timed out."""
        for attempt in (1, 2):
            try:
                return await self._get_json(
                    "/api/v1/pikpak/download", params, "download"
                )
            except PikPakError as exc:
                if (
                    exc.status not in _DOWNLOAD_RETRY_STATUSES
                    or attempt == 2
                ):
                    raise
                delay = min(exc.retry_after or 2.0, _MAX_RETRY_DELAY)
                LOGGER.info(
                    "PikPak download: %s, retrying in %.1fs", exc, delay
                )
                await sleep(delay)
        raise PikPakError("PikPak gateway error")

    async def _share_json(self, params: dict[str, str]) -> dict[str, Any]:
        reason = "PikPak gateway error"
        for attempt in range(1, _SHARE_ATTEMPTS + 1):
            try:
                return await self._get_json(
                    "/api/v1/pikpak/share", params, "share"
                )
            except PikPakError as exc:
                reason = str(exc)
                if (
                    exc.status not in _SHARE_RETRY_STATUSES
                    or attempt == _SHARE_ATTEMPTS
                ):
                    raise
                delay = min(
                    exc.retry_after or float(2 ** (attempt - 1)),
                    _MAX_RETRY_DELAY,
                )
                LOGGER.info(
                    "PikPak share: %s, retrying in %.1fs [%d/%d]",
                    exc,
                    delay,
                    attempt,
                    _SHARE_ATTEMPTS,
                )
                await sleep(delay)
        raise PikPakError(reason)

    async def _get_json(
        self, path: str, params: dict[str, str], action: str
    ) -> dict[str, Any]:
        try:
            response = await self._http.get(
                gateway_url(path),
                params=params,
                headers=gateway_headers(),
            )
        except httpx.TimeoutException as exc:
            raise PikPakError(
                "PikPak gateway timed out. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            LOGGER.warning("PikPak %s request failed: %r", action, exc)
            raise PikPakError(
                "PikPak gateway could not be reached. Try again."
            ) from exc
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise _gateway_error(
                response.status_code,
                body,
                retry_after=_retry_after(response, body),
                action=action,
            )
        if not body:
            LOGGER.error(
                "PikPak %s: unreadable response (HTTP %s)",
                action,
                response.status_code,
            )
            raise PikPakError("PikPak gateway sent an unreadable response.")
        if not body.get("success"):
            raise _gateway_error(response.status_code, body, action=action)
        return body


def _check_cdn_status(response: httpx.Response, url: str) -> None:
    """Turn a CDN answer into the failure the user should see."""
    if response.status_code in (404, 410):
        raise PikPakError("PikPak download link expired.", stale=True)
    if response.status_code >= 400:
        LOGGER.warning(
            "PikPak CDN answered HTTP %s for %s",
            response.status_code,
            url.split("?", 1)[0],
        )
        raise PikPakError(f"PikPak CDN answered HTTP {response.status_code}.")


def _retry_after(response: httpx.Response, body: dict[str, Any]) -> float:
    value: Any = body.get("retry_after")
    if value is None:
        value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _gateway_error(
    status: int,
    body: dict[str, Any],
    *,
    retry_after: float = 0.0,
    action: str = "request",
) -> PikPakError:
    raw = str(body.get("error") or "").strip()
    lowered = raw.lower()
    stale = any(hint in lowered for hint in _STALE_HINTS)
    if status in (401, 403):
        message = (
            "PikPak gateway rejected the API token. "
            "Check GATEWAY_TOKEN in the bot configuration."
        )
    elif status == 429:
        wait = f" Try again in {int(retry_after)}s." if retry_after else ""
        message = f"PikPak gateway is rate-limiting the bot.{wait}"
    elif any(hint in lowered for hint in _PASSWORD_HINTS):
        message = (
            "This PikPak share needs a password. "
            "Send the link with ?pwd=<code> appended."
        )
    elif stale:
        message = "This PikPak share has expired or was removed."
    elif status == 404:
        message = "PikPak gateway endpoint not found. Is the gateway up to date?"
    elif status >= 500:
        message = f"PikPak gateway failed: {raw or f'HTTP {status}'}."
    else:
        message = f"PikPak gateway rejected the request: {raw or f'HTTP {status}'}."
    LOGGER.warning(
        "PikPak %s: HTTP %s %s", action, status, raw[:500] or "(no body)"
    )
    return PikPakError(
        message, stale=stale, status=status, retry_after=retry_after
    )
