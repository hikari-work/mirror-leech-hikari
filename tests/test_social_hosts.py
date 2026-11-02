"""x / instagram / tiktok hosts: what one post turns into.

Each handler reads one gateway endpoint and hands the direct downloader a
``contents`` list. These tests pin what that list is made of: the media order,
the container each file gets, TikTok's no-watermark choice and the header set
its CDN demands, and the classification that decides whether a refusal is
retried. The gateway is a scripted session, so no network is involved.
"""

from __future__ import annotations

import importlib.util
import sys
from os import path as ospath
from pathlib import Path
from re import sub as resub
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent

class _Logger:
    def __init__(self):
        self.messages: list[str] = []

    def _record(self, msg):
        self.messages.append(str(msg))

    info = warning = error = debug = _record

class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

class _Session:
    """Replays canned responses in order; the last one repeats."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append({"url": url, "params": params or {}})
        item = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

@pytest.fixture
def social(monkeypatch):
    """Load ``hosts/social.py`` with its package stubbed out."""
    pkg = ModuleType("social_stub")
    pkg.__path__ = []
    hosts_pkg = ModuleType("social_stub.hosts")
    hosts_pkg.__path__ = []

    class DirectDownloadLinkException(Exception):
        pass

    logger = _Logger()

    def safe_name(name, fallback):
        cleaned = resub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip()).strip(" .")
        return cleaned[:200] or fallback

    def safe_stem(title, fallback):
        stem, ext = ospath.splitext(safe_name(title, fallback))
        if ext.lower() in common.MEDIA_EXTS:
            return stem
        return safe_name(title, fallback)

    def unique_stem(stem, taken, code=""):
        if (key := stem.lower()) not in taken:
            taken.add(key)
            return stem
        stem = f"{stem} {code}" if code else f"{stem} {len(taken)}"
        taken.add(stem.lower())
        return stem

    def header_lines(headers):
        return [f"{key}: {value}" for key, value in sorted(headers.items())]

    common = ModuleType("social_stub._common")
    common.LOGGER = logger
    common.DirectDownloadLinkException = DirectDownloadLinkException
    common.MEDIA_EXTS = frozenset((".mp4", ".mkv", ".webm", ".m4v", ".m3u8"))
    common.safe_name = safe_name
    common.safe_stem = safe_stem
    common.unique_stem = unique_stem
    common.header_lines = header_lines
    common.gateway_url = lambda path="": f"https://gateway.test{path}"
    common.gateway_headers = lambda accept_json=True: {"accept": "application/json"}

    registry = ModuleType("social_stub.registry")
    registry.register = lambda **kwargs: (lambda func: func)

    for name, mod in {
        "social_stub": pkg,
        "social_stub.hosts": hosts_pkg,
        "social_stub._common": common,
        "social_stub.registry": registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    path = (
        _ROOT / "bot" / "helper" / "download" / "direct_link_generators"
        / "hosts" / "social.py"
    )
    spec = importlib.util.spec_from_file_location("social_stub.hosts.social", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "social_stub.hosts.social", module)
    spec.loader.exec_module(module)
    module.LOGGER = logger
    return module

def _use(module, monkeypatch, *responses):
    session = _Session(*responses)
    monkeypatch.setattr(module, "Session", lambda: session, raising=False)
    monkeypatch.setattr(module, "sleep", lambda seconds: None, raising=False)
    return session

def _ok(data):
    return _Resp({"success": True, "data": data})

def _fail(reason, status=500):
    return _Resp({"success": False, "error": reason}, status)

def _x_payload():
    return {
        "id": "910031516746514432",
        "text": "[Direct] #Maria",
        "media": [
            {
                "index": 0,
                "kind": "video",
                "url": "https://video.twimg.com/1280x720/p.mp4",
                "variants": ["https://video.twimg.com/320x180/p.mp4"],
            },
            {
                "index": 1,
                "kind": "photo",
                "url": "https://pbs.twimg.com/media/a.jpg?name=orig",
            },
        ],
        "download": {"headers": {"user-agent": "Googlebot"}},
    }

def test_host_matching_is_exact_or_subdomain(social):
    """A substring test would claim netflix.com for the x handler."""
    assert social.is_x_link("https://x.com/a/status/20")
    assert social.is_x_link("https://mobile.twitter.com/a/status/20")
    assert not social.is_x_link("https://netflix.com/title/1")
    assert social.is_instagram_link("https://www.instagram.com/p/abc/")
    assert not social.is_instagram_link("https://notinstagram.com/p/abc/")
    assert social.is_tiktok_link("https://vm.tiktok.com/ZMabc/")
    assert not social.is_tiktok_link("https://example.com/@a/video/1")

def test_a_tweet_becomes_one_entry_per_media_item(social, monkeypatch):
    session = _use(social, monkeypatch, _ok(_x_payload()))

    result = social.x_post("https://x.com/a/status/910031516746514432")

    assert session.calls[0]["url"] == "https://gateway.test/api/v1/scrape/x"
    assert session.calls[0]["params"] == {"q": "https://x.com/a/status/910031516746514432"}
    assert [c["url"] for c in result["contents"]] == [
        "https://video.twimg.com/1280x720/p.mp4",
        "https://pbs.twimg.com/media/a.jpg?name=orig",
    ]
    # the container comes off the URL's path, and the signed query is not part
    # of the extension
    assert [ospath.splitext(c["filename"])[1] for c in result["contents"]] == [
        ".mp4",
        ".jpg",
    ]
    assert result["total_size"] == 0
    assert result["header"] == ["user-agent: Googlebot"]

def test_a_carousel_keeps_every_item_in_order(social, monkeypatch):
    _use(
        social,
        monkeypatch,
        _ok(
            {
                "shortcode": "BQ0eAlwhDrw",
                "caption": "a caption",
                "media": [
                    {"index": 0, "kind": "video", "url": "https://cdn/1.mp4"},
                    {"index": 1, "kind": "image", "url": "https://cdn/2.jpg"},
                ],
                "download": {"headers": {"referer": "https://www.instagram.com/"}},
            }
        ),
    )

    result = social.instagram_post("https://www.instagram.com/p/BQ0eAlwhDrw/")

    assert [c["filename"] for c in result["contents"]] == [
        "a caption.mp4",
        "a caption 1.jpg",
    ]
    assert result["header"] == ["referer: https://www.instagram.com/"]

def test_a_post_with_no_media_is_an_error(social, monkeypatch):
    _use(
        social,
        monkeypatch,
        _ok({"id": "20", "text": "just setting up my twttr", "media": []}),
    )

    with pytest.raises(Exception) as excinfo:
        social.x_post("https://x.com/a/status/20")
    assert "no photo or video" in str(excinfo.value)

def test_tiktok_prefers_the_watermark_free_rendition(social, monkeypatch):
    _use(
        social,
        monkeypatch,
        _ok(
            {
                "id": "7106594312292453675",
                "desc": "how many frogs",
                "video": {
                    "play_url": "https://cdn/play.mp4",
                    "download_url": "https://cdn/download.mp4",
                },
                "headers": {
                    "Cookie": "tt_chain_token=abc",
                    "Referer": "https://www.tiktok.com/",
                },
            }
        ),
    )

    result = social.tiktok_post("https://www.tiktok.com/@tiktok/video/7106594312292453675")

    assert [c["url"] for c in result["contents"]] == ["https://cdn/play.mp4"]
    assert result["contents"][0]["filename"] == "how many frogs.mp4"
    assert result["header"] == [
        "Cookie: tt_chain_token=abc",
        "Referer: https://www.tiktok.com/",
    ]

def test_a_tiktok_slideshow_is_one_entry_per_frame(social, monkeypatch):
    _use(
        social,
        monkeypatch,
        _ok(
            {
                "id": "7297622340506483974",
                "kind": "photo",
                "desc": "frames",
                "images": [
                    {"url": "https://cdn/1.jpeg", "width": 1080, "height": 1350},
                    {"url": "https://cdn/2.jpeg"},
                ],
            }
        ),
    )

    result = social.tiktok_post("https://www.tiktok.com/@tiktok/photo/7297622340506483974")

    assert [c["url"] for c in result["contents"]] == [
        "https://cdn/1.jpeg",
        "https://cdn/2.jpeg",
    ]
    assert [c["filename"] for c in result["contents"]] == [
        "frames.jpeg",
        "frames 1.jpeg",
    ]
    assert result["header"] == []

def test_a_gateway_hiccup_is_retried_without_the_cached_answer(social, monkeypatch):
    """The first attempt is served from the gateway's cache; only the retry
    asks for a fresh answer, which is what ``nocache`` is for."""
    session = _use(
        social, monkeypatch, _fail("upstream exploded", 502), _ok(_x_payload())
    )

    result = social.x_post("https://x.com/a/status/910031516746514432")

    assert len(result["contents"]) == 2
    assert [call["params"].get("nocache") for call in session.calls] == [None, "1"]

def test_a_dead_post_is_not_retried(social, monkeypatch):
    session = _use(
        social,
        monkeypatch,
        _Resp({"success": False, "error": "tweet 1 is not available"}, 404),
    )

    with pytest.raises(Exception) as excinfo:
        social.x_post("https://x.com/a/status/1")
    assert "not available" in str(excinfo.value)
    assert len(session.calls) == 1
