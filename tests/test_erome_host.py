"""Erome host: gateway read and the ``contents`` list the downloader consumes.

An erome album is a mix of images and videos, and a profile is a list of
albums, so the handler has two jobs beyond the request: every media entry the
gateway returns has to survive into ``contents`` with its filename intact, and
a profile's files have to land in their own album folder -- two albums can
easily share a file name, and a flat listing would overwrite one with the
other.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    """Replays one canned response and records what was asked for it."""

    def __init__(self, response):
        self.response = response
        self.params: list[dict] = []

    def get(self, url, **kwargs):
        self.params.append(kwargs.get("params") or {})
        return self.response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def erome(monkeypatch):
    """Load ``hosts/erome.py`` with its package stubbed out.

    Same recipe as the ``doodstream`` fixture: the real package pulls in the
    whole generator chain, and erome only needs the gateway helpers plus the
    three name helpers whose behaviour the tests pin.
    """
    path = (
        _ROOT / "bot" / "helper" / "download" / "direct_link_generators" / "hosts"
        / "erome.py"
    )
    pkg = ModuleType("erome_stub")
    pkg.__path__ = []
    hosts_pkg = ModuleType("erome_stub.hosts")
    hosts_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        error = warning = debug = info

    class DirectDownloadLinkException(Exception):
        pass

    def _safe_name(name, fallback):
        from re import sub

        cleaned = sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip()).strip(" .")
        return cleaned[:200] or fallback

    def _safe_stem(title, fallback):
        from os import path as ospath

        stem, ext = ospath.splitext(_safe_name(title, fallback))
        media = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv"}
        return stem if ext.lower() in media else _safe_name(title, fallback)

    def _unique_stem(stem, taken, code=""):
        if (key := stem.lower()) not in taken:
            taken.add(key)
            return stem
        stem = f"{stem} {code}" if code else f"{stem} {len(taken)}"
        taken.add(stem.lower())
        return stem

    common = ModuleType("erome_stub._common")
    common.LOGGER = _Logger()
    common.DirectDownloadLinkException = DirectDownloadLinkException
    common.safe_name = _safe_name
    common.safe_stem = _safe_stem
    common.unique_stem = _unique_stem
    common.gateway_url = lambda path="": f"https://gateway.test{path}"
    common.gateway_headers = lambda accept_json=True: {"accept": "application/json"}
    registry = ModuleType("erome_stub.registry")
    registry.register = lambda *args, **kwargs: (lambda func: func)
    for name, mod in {
        "erome_stub": pkg,
        "erome_stub.hosts": hosts_pkg,
        "erome_stub._common": common,
        "erome_stub.registry": registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("erome_stub.hosts.erome", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "erome_stub.hosts.erome", module)
    spec.loader.exec_module(module)
    return module


def _album_body():
    return {
        "success": True,
        "kind": "album",
        "album": {"album_id": "FVJdknNf", "title": "Sexy Lil Lightskin"},
        "total_media": 2,
        "media": [
            {
                "url": "https://v86.erome.com/1/FVJdknNf/clip_720p.mp4",
                "type": "video",
                "filename": "clip_720p.mp4",
                "album_id": "FVJdknNf",
            },
            {
                "url": "https://s86.erome.com/1/FVJdknNf/photo.jpg?v=1",
                "type": "image",
                "filename": "photo.jpg",
                "album_id": "FVJdknNf",
            },
        ],
        "profile": "",
    }


def _profile_body():
    return {
        "success": True,
        "kind": "profile",
        "album": None,
        "profile": "Some Uploader",
        "total_media": 2,
        "media": [
            {
                "url": "https://v86.erome.com/1/AAA111/first.mp4",
                "type": "video",
                "filename": "clip.mp4",
                "album_id": "AAA111",
            },
            {
                "url": "https://v86.erome.com/1/BBB222/first.mp4",
                "type": "video",
                "filename": "clip.mp4",
                "album_id": "BBB222",
            },
        ],
    }


# ── matching ─────────────────────────────────────────────────────────
def test_matches_the_host_and_its_subdomains(erome):
    assert erome.is_erome_link("https://www.erome.com/a/FVJdknNf")
    assert erome.is_erome_link("https://erome.com/a/FVJdknNf")
    assert not erome.is_erome_link("https://noterome.com/a/FVJdknNf")
    assert not erome.is_erome_link("https://erome.com.evil.test/a/FVJdknNf")


# ── the descriptor the direct downloader consumes ────────────────────
def test_album_keeps_images_and_videos_in_one_folder(erome, monkeypatch):
    session = _Session(_Resp(_album_body()))
    monkeypatch.setattr(erome, "Session", lambda: session)
    resolved = erome.erome("https://www.erome.com/a/FVJdknNf")
    assert resolved["title"] == "Sexy Lil Lightskin"
    assert resolved["total_size"] == 0
    assert resolved["contents"] == [
        {
            "path": "",
            "filename": "clip_720p.mp4",
            "url": "https://v86.erome.com/1/FVJdknNf/clip_720p.mp4",
        },
        {
            "path": "",
            "filename": "photo.jpg",
            "url": "https://s86.erome.com/1/FVJdknNf/photo.jpg?v=1",
        },
    ]
    # the album URL travels in ``q``, untouched
    assert session.params[0]["q"] == "https://www.erome.com/a/FVJdknNf"


def test_profile_files_keep_their_album_folder(erome, monkeypatch):
    monkeypatch.setattr(erome, "Session", lambda: _Session(_Resp(_profile_body())))
    resolved = erome.erome("https://www.erome.com/Some_Uploader")
    assert resolved["title"] == "Some Uploader"
    assert [entry["path"] for entry in resolved["contents"]] == ["AAA111", "BBB222"]
    # same file name in two albums must not collapse into one file
    assert {entry["filename"] for entry in resolved["contents"]} == {"clip.mp4"}


def test_two_same_named_files_in_one_album_get_distinct_names(erome, monkeypatch):
    body = _album_body()
    body["media"][1]["filename"] = "clip_720p.mp4"
    monkeypatch.setattr(erome, "Session", lambda: _Session(_Resp(body)))
    resolved = erome.erome("https://www.erome.com/a/FVJdknNf")
    names = [entry["filename"] for entry in resolved["contents"]]
    assert len(names) == len(set(names)) == 2


def test_gateway_error_fails_the_task_with_its_reason(erome, monkeypatch):
    body = {"success": False, "error": "fetching album: HTTP 404"}
    monkeypatch.setattr(erome, "Session", lambda: _Session(_Resp(body)))
    with pytest.raises(Exception) as excinfo:
        erome.erome("https://www.erome.com/a/gone")
    assert "fetching album: HTTP 404" in str(excinfo.value)


def test_album_without_media_is_an_error(erome, monkeypatch):
    body = {"success": True, "kind": "album", "album": {"title": "x"}, "media": []}
    monkeypatch.setattr(erome, "Session", lambda: _Session(_Resp(body)))
    with pytest.raises(Exception) as excinfo:
        erome.erome("https://www.erome.com/a/empty")
    assert "no media" in str(excinfo.value)
