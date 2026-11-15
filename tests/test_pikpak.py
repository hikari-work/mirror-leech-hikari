"""PikPak gateway client and sequential downloader, gateway mocked with httpx."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import time

import httpx
import pytest

from bot.core.config_manager import Config
from bot.helper.download import pikpak_download
from bot.helper.net import pikpak_client
from bot.helper.net.pikpak_client import (
    PikPakClient,
    PikPakEntry,
    PikPakError,
    parse_share_link,
    sanitize_filename,
)

SHARE = "https://mypikpak.com/s/VP45AOkZd6hLOKNtqrKemsUKo2"
API = "https://api.piyann.my.id"


@pytest.fixture(autouse=True)
def fresh_lock(monkeypatch):
    """One module lock per test loop: production runs one loop, pytest does not."""
    monkeypatch.setattr(pikpak_download, "PIKPAK_DOWNLOAD_LOCK", asyncio.Lock())


def _client(handler) -> PikPakClient:
    return PikPakClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ---------------------------------------------------------------------------
# Client contract, as read from the gateway's src/pikpak/handlers.rs
# ---------------------------------------------------------------------------


async def test_share_reads_query_files_and_bearer_token(monkeypatch):
    monkeypatch.setattr(Config, "GATEWAY_TOKEN", "test-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "share_id": "VP45AOkZd6hLOKNtqrKemsUKo2",
                "total_files": 2,
                "files": [
                    {
                        "id": "Vfile1",
                        "name": "clip.mp4",
                        "kind": "drive#file",
                        "is_folder": False,
                        "size": 12,
                        "mime_type": "video/mp4",
                        "parent_id": "",
                    },
                    {
                        "id": "Vfolder",
                        "name": "sub",
                        "kind": "drive#folder",
                        "is_folder": True,
                        "size": 0,
                        "mime_type": "",
                        "parent_id": "",
                    },
                ],
                "error": "",
                "timestamp": 1,
                "duration_ms": 1.0,
            },
        )

    async with _client(handler) as client:
        entries = await client.share(SHARE)

    assert entries == [
        PikPakEntry(id="Vfile1", name="clip.mp4", size=12, is_folder=False),
        PikPakEntry(id="Vfolder", name="sub", size=0, is_folder=True),
    ]
    assert seen[0].url.path == "/api/v1/pikpak/share"
    assert seen[0].url.params["url"] == SHARE
    assert seen[0].headers["authorization"] == "Bearer test-token"


async def test_download_reads_filename_from_response_not_listing(monkeypatch):
    monkeypatch.setattr(Config, "GATEWAY_TOKEN", "")
    seen: list[httpx.Request] = []
    name = "@ストッキングビリビリにされちゃった_restored.mp4"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "download_url": "https://cdn.test/file",
                "file_id": "Vdriveid",
                "filename": name,
                "file_size": 99,
                "expires_at": int(time()) + 3600,
                "purged": 1,
                "error": "",
                "timestamp": 1,
                "duration_ms": 1.0,
            },
        )

    async with _client(handler) as client:
        info = await client.download(SHARE, "Vfile1")

    assert info.filename == name
    assert info.file_id == "Vdriveid"
    assert info.size == 99
    assert seen[0].url.path == "/api/v1/pikpak/download"
    assert seen[0].url.params["url"] == SHARE
    assert seen[0].url.params["file_id"] == "Vfile1"


async def test_password_error_is_explained_to_the_user(monkeypatch):
    monkeypatch.setattr(Config, "GATEWAY_TOKEN", "")
    body = {
        "success": False,
        "share_id": "Vabc",
        "total_files": 0,
        "files": [],
        "error": "pikpak share Vabc is PASS_CODE_NEEDED PASS_CODE_NEEDED",
        "timestamp": 1,
        "duration_ms": 1.0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=body)

    async with _client(handler) as client:
        with pytest.raises(PikPakError) as caught:
            await client.share(SHARE)

    assert "password" in str(caught.value)
    assert "?pwd=" in str(caught.value)
    assert caught.value.status == 500


async def test_download_to_streams_bytes_and_marks_gone_stale(tmp_path):
    payload = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/missing":
            return httpx.Response(410)
        return httpx.Response(200, content=payload)

    async with _client(handler) as client:
        sink = tmp_path / "_pikpak_stream.bin"
        chunks: list[int] = []
        try:
            written = await client.download_to(
                f"{API}/file", str(sink), on_bytes=chunks.append
            )
            assert sink.read_bytes() == payload
        finally:
            sink.unlink(missing_ok=True)
        assert written == len(payload)
        assert sum(chunks) == len(payload)
        with pytest.raises(PikPakError) as caught:
            await client.download_to(f"{API}/missing", str(sink))
        assert caught.value.stale


async def test_download_to_ranges_one_result_over_many_connections(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(pikpak_client, "_MIN_SPLIT", 1024)
    monkeypatch.setattr(pikpak_client, "_CONNECTIONS", 4)
    payload = bytes(range(256)) * 16  # 4096 bytes
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("range", "")
        ranges.append(header)
        start, end = (int(part) for part in header.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=payload[start : end + 1])

    dest = tmp_path / "ranged.bin"
    async with _client(handler) as client:
        written = await client.download_to(
            f"{API}/file", str(dest), size=len(payload)
        )

    assert written == len(payload)
    assert dest.read_bytes() == payload
    assert len(ranges) == 4
    assert sorted(ranges) == [
        "bytes=0-1023",
        "bytes=1024-2047",
        "bytes=2048-3071",
        "bytes=3072-4095",
    ]


async def test_range_ignored_falls_back_to_one_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(pikpak_client, "_MIN_SPLIT", 1024)
    monkeypatch.setattr(pikpak_client, "_CONNECTIONS", 4)
    payload = b"y" * 4096
    ranged: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range"):
            ranged.append(request.headers["range"])
        return httpx.Response(200, content=payload)

    dest = tmp_path / "fallback.bin"
    async with _client(handler) as client:
        written = await client.download_to(
            f"{API}/file", str(dest), size=len(payload)
        )

    assert ranged, "the ranged attempt must happen before the fallback"
    assert written == len(payload)
    assert dest.read_bytes() == payload


# ---------------------------------------------------------------------------
# Link and filename rules
# ---------------------------------------------------------------------------


def test_share_link_is_validated_against_the_mypikpak_pattern():
    assert parse_share_link(f"  {SHARE}  ") == SHARE
    assert (
        parse_share_link(f"{SHARE}?pwd=a9z2") == f"{SHARE}?pwd=a9z2"
    )
    for bad in (
        "https://example.com/s/Vabc",
        "https://mypikpak.com/pikpak",
        "https://mypikpak.com/s/",
        "mypikpak.com/s/Vabc",
    ):
        with pytest.raises(PikPakError):
            parse_share_link(bad)


def test_sanitize_keeps_gateway_unicode_and_at_prefix():
    name = "@ストッキングビリビリにされちゃった_restored.mp4"
    assert sanitize_filename(name) == name
    assert sanitize_filename("cafe\u0301.mp4") == "café.mp4"


async def test_leech_resolver_parses_share_and_rejects_bad_path():
    from bot.helper.download.link_resolver import resolve_pikpak

    class _Listener:
        def __init__(self, link: str) -> None:
            self.link = link
            self.failed: list[str] = []

        async def fail_task(self, message, notify=True):
            self.failed.append(str(message))

    listener = _Listener(SHARE)
    assert await resolve_pikpak(listener) is True
    assert listener.link["pikpak"]["url"] == SHARE
    assert listener.link["pikpak"]["share_id"] == "VP45AOkZd6hLOKNtqrKemsUKo2"

    bad = _Listener("https://mypikpak.com/foo")
    assert await resolve_pikpak(bad) is False
    assert "Invalid PikPak link" in bad.failed[0]


def test_sanitize_removes_paths_controls_and_caps_length():
    hostile = sanitize_filename("../../etc\x00/pässwd\n")
    assert "/" not in hostile and "\\" not in hostile
    assert ".." not in hostile and "\x00" not in hostile
    assert "\n" not in hostile
    long_name = sanitize_filename("あ" * 200 + ".mp4")
    assert long_name.endswith(".mp4")
    assert len(long_name.encode("utf-8")) <= 200


# ---------------------------------------------------------------------------
# Sequential downloader: one lock, one stale retry
# ---------------------------------------------------------------------------


class _Tracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        self.active -= 1


class _FakeClient:
    def __init__(self, tracker: _Tracker, files: list[PikPakEntry]) -> None:
        self.tracker = tracker
        self.files = files
        self.share_calls: list[bool] = []
        self.downloads: list[str] = []

    async def share(self, url: str, *, fresh: bool = False):
        self.share_calls.append(fresh)
        return list(self.files)

    async def download(self, url: str, file_id: str):
        from bot.helper.net.pikpak_client import PikPakDownload

        self.downloads.append(file_id)
        self.tracker.enter()
        try:
            await asyncio.sleep(0.01)
            return PikPakDownload(
                url=f"https://cdn.test/{file_id}",
                file_id=file_id,
                filename=f"{file_id}.bin",
                size=4,
                expires_at=0,
            )
        finally:
            self.tracker.leave()

    async def download_to(
        self,
        url: str,
        dest: str,
        *,
        size: int = 0,
        on_bytes=None,
        is_cancelled=None,
    ) -> int:
        self.tracker.enter()
        try:
            await asyncio.sleep(0.01)
            data = b"data"
            Path(dest).write_bytes(data)
            if on_bytes is not None:
                on_bytes(len(data))
            return len(data)
        finally:
            self.tracker.leave()


class _FakeMessage:
    """Stands in for the chat message; records anything the helper sends."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def reply_rich(self, *args, **kwargs):
        self.sent.append(str(kwargs))


class _FakeListener:
    def __init__(self) -> None:
        self.link: dict = {}
        self.name = ""
        self.size = 0
        self.is_cancelled = False
        self.message = None
        self.multi = 1
        self.is_rss = False
        self.mid = 1
        self.errors: list[str] = []

    async def on_download_error(self, error, button=None):
        self.errors.append(str(error))


def _helper(client, files: list[PikPakEntry]) -> pikpak_download.PikPakDownloadHelper:
    listener = _FakeListener()
    helper = pikpak_download.PikPakDownloadHelper(listener, client=client)
    helper._single_file = len(files) == 1
    return helper


async def test_two_tasks_never_overlap_a_download_call(tmp_path):
    files = [
        PikPakEntry("V1", "one.bin", 4, False),
        PikPakEntry("V2", "two.bin", 4, False),
    ]
    tracker = _Tracker()
    first = _helper(_FakeClient(tracker, files), files)
    second = _helper(_FakeClient(tracker, files), files)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    await asyncio.gather(
        first._download_files(SHARE, files, str(tmp_path / "a")),
        second._download_files(SHARE, files, str(tmp_path / "b")),
    )

    assert tracker.peak == 1
    assert (tmp_path / "a" / "V1.bin").read_bytes() == b"data"
    assert (tmp_path / "b" / "V2.bin").read_bytes() == b"data"


async def test_stale_result_re_lists_and_retries_once(tmp_path):
    files = [PikPakEntry("V1", "one.bin", 4, False)]
    client = _FakeClient(_Tracker(), files)
    original = client.download
    calls = {"n": 0}

    async def stale_first(url: str, file_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PikPakError("PikPak download link expired.", stale=True)
        return await original(url, file_id)

    client.download = stale_first
    helper = _helper(client, files)

    failures = await helper._download_files(SHARE, files, str(tmp_path))

    assert failures == []
    assert client.share_calls == [True]
    assert (tmp_path / "V1.bin").read_bytes() == b"data"


async def test_downloads_send_no_per_file_chat_messages(tmp_path):
    files = [
        PikPakEntry("V1", "one.bin", 4, False),
        PikPakEntry("V2", "two.bin", 4, False),
    ]
    helper = _helper(_FakeClient(_Tracker(), files), files)
    helper._listener.message = _FakeMessage()

    failures = await helper._download_files(SHARE, files, str(tmp_path))

    assert failures == []
    assert helper._listener.message.sent == []


async def test_second_stale_answer_is_reported_not_looped(tmp_path):
    files = [PikPakEntry("V1", "one.bin", 4, False)]
    client = _FakeClient(_Tracker(), files)
    calls = {"n": 0}

    async def always_stale(url: str, file_id: str):
        calls["n"] += 1
        raise PikPakError("PikPak download link expired.", stale=True)

    client.download = always_stale
    helper = _helper(client, files)

    failures = await helper._download_files(SHARE, files, str(tmp_path))

    assert calls["n"] == 2
    assert client.share_calls == [True]
    assert len(failures) == 1
    assert not (tmp_path / "V1.bin").exists()
