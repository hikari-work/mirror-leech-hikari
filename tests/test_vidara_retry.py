"""Tests for Vidara's retry classification.

The resolver used to call anything short of HTTP 5xx final, so the 429 a bulk
provokes was reported as "rejected by the API" -- indistinguishable from a video
that was actually removed, and counted as a failed link. These tests pin which
answers are worth another attempt, and that the backoff a retry waits out grows
and carries jitter (a batch resolves in lockstep, so a fixed sleep only
reschedules the same burst).
"""

from __future__ import annotations

import pytest

# The ``vidara`` fixture lives in conftest.py: test_vidara_folder.py loads the
# same module the same way.


class _Resp:
    def __init__(self, status, payload, headers=None, text="", url=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Session:
    """Replays canned responses; the last one repeats once the queue runs dry."""

    def __init__(self, *responses):
        self._q = list(responses)
        self.calls = 0
        self.params = []

    def get(self, url, **kwargs):
        self.calls += 1
        self.params.append(kwargs.get("params") or {})
        item = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_body(master="https://cdn.test/master.m3u8", page_url=""):
    body = {"success": True, "master_url": master, "title": "clip.mp4"}
    if page_url:
        body["page_url"] = page_url
    return body


def _hls_resp(url="https://cdn.test/master.m3u8"):
    """What the HLS probe in the registered handler expects to fetch."""
    return _Resp(
        200,
        None,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n",
        url=url,
    )

class _MintSession(_Session):
    """``_Session`` plus the stream-API POST the resolver mints a token with."""

    def __init__(self, *responses, stream="", error=None):
        super().__init__(*responses)
        self.stream = stream
        self.error = error
        self.posted: list[tuple[str, str]] = []

    def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        self.posted.append((url, body.get("filecode") or ""))
        if self.error:
            raise self.error
        return _Resp(200, {"streaming_url": self.stream})


# ── which answers are retryable ──────────────────────────────────────


def test_rate_limit_is_retryable(vidara):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"},
                             {"Retry-After": "4"}))

    response, reason, retryable, retry_after = vidara.vidara_scrape(
        session, "https://vidara.to/abc"
    )

    assert response is None
    assert retryable is True
    assert retry_after == "4"


def test_server_error_is_retryable(vidara):
    session = _Session(_Resp(503, None))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is True
    assert "503" in reason


def test_rate_limit_wrapped_in_a_200_is_retryable(vidara):
    """The gateway also reports throttling as HTTP 200 + success: false."""
    session = _Session(_Resp(200, {"success": False, "error": "Rate limit hit"}))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is True
    assert reason == "Rate limit hit"


def test_removed_video_is_final(vidara):
    session = _Session(_Resp(404, {"success": False, "error": "video not found"}))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is False
    assert reason == "video not found"


def test_connection_error_is_retryable(vidara):
    session = _Session(OSError("connection reset"))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is True
    assert reason == "OSError"


# ── backoff ──────────────────────────────────────────────────────────


def test_retry_delay_backs_off_and_stays_capped(vidara):
    assert 2 <= vidara.vidara_retry_delay(1) <= 3.5
    assert 4 <= vidara.vidara_retry_delay(2) <= 5.5
    assert vidara.vidara_retry_delay(20) <= vidara.VIDARA_MAX_RETRY_DELAY + 1.5


def test_retry_delay_honours_retry_after(vidara):
    assert 6 <= vidara.vidara_retry_delay(1, "6") <= 7.5
    # a header the gateway sent as a date, or as nonsense, must not raise
    assert 2 <= vidara.vidara_retry_delay(1, "Wed, 21 Oct 2015 07:28:00 GMT") <= 3.5


def test_retry_delay_is_jittered(vidara):
    delays = {round(vidara.vidara_retry_delay(1), 6) for _ in range(20)}
    assert len(delays) > 1


# ── the retry loop ───────────────────────────────────────────────────


def test_resolve_retries_a_rate_limit_then_succeeds(vidara, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"}),
                       _Resp(200, _ok_body()))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    name, link, headers = vidara.vidara_resolve("https://vidara.to/abc")

    assert name == "clip"
    assert link == "https://cdn.test/master.m3u8"
    assert headers["Referer"]
    assert session.calls == 2


def test_resolve_gives_up_after_its_attempts(vidara, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"}))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    with pytest.raises(vidara.DirectDownloadLinkException, match="slow down"):
        vidara.vidara_resolve("https://vidara.to/abc")

    assert session.calls == vidara.VIDARA_ATTEMPTS


def test_resolve_does_not_retry_a_dead_link(vidara, monkeypatch):
    session = _Session(_Resp(404, {"success": False, "error": "video not found"}))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    with pytest.raises(vidara.DirectDownloadLinkException, match="video not found"):
        vidara.vidara_resolve("https://vidara.to/abc")

    assert session.calls == 1


def test_only_a_retry_bypasses_the_gateway_cache(vidara, monkeypatch):
    """The gateway stores a failed answer past this module's retry window.

    Without ``nocache`` on the retries, every attempt is served the same stored
    failure and a one-off hiccup reads as a dead video.
    """
    session = _Session(_Resp(429, {"success": False, "error": "slow down"}),
                       _Resp(200, _ok_body()))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    vidara.vidara_resolve("https://vidara.to/abc")

    assert "nocache" not in session.params[0]
    assert session.params[1]["nocache"] == "1"


def test_handler_retries_a_rate_limit_then_returns_an_ytdlp_link(vidara, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "too many requests"}),
                       _Resp(200, _ok_body()),
                       _hls_resp())
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    resolved = vidara.vidara("https://vidara.to/abc")

    assert resolved["ytdlp"] is True
    assert resolved["link"] == "https://cdn.test/master.m3u8"
    # two scrapes plus the playlist probe
    assert session.calls == 3


def test_handler_does_not_retry_a_dead_link(vidara, monkeypatch):
    session = _Session(_Resp(410, {"success": False, "error": "video removed"}))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    with pytest.raises(vidara.DirectDownloadLinkException, match="video removed"):
        vidara.vidara("https://vidara.to/abc")

    assert session.calls == 1

# ── the token, and which IP may use it ───────────────────────────────

def test_resolve_mints_the_stream_here_not_at_the_gateway(vidara, monkeypatch):
    """The CDN token is signed for the IP that minted it, and answers 404 from
    any other. A URL the gateway minted is only good from the gateway, so the
    bot used to hand yt-dlp a playlist that was already refused here."""
    minted = "https://cdn.test/master.m3u8?token=here"
    session = _MintSession(
        _Resp(200, _ok_body(page_url="https://vidara.to/v/abc")),
        stream=minted,
    )
    monkeypatch.setattr(vidara, "Session", lambda: session)

    name, link, headers = vidara.vidara_resolve("https://vidara.to/abc")

    assert name == "clip"
    assert link == minted
    assert headers["Referer"]
    assert session.posted == [("https://vidara.to/api/stream", "abc")]

def test_handler_probes_a_local_token_after_the_gateway_one_is_refused(
    vidara, monkeypatch
):
    """A 404 on the gateway's URL is the IP, not a dead video: mint here, retry
    the probe, and only report a rejection if that fails too."""
    minted = "https://cdn.test/master.m3u8?token=here"
    session = _MintSession(
        _Resp(200, _ok_body(page_url="https://vidara.to/v/abc")),
        _Resp(404, None),
        _hls_resp(minted),
        stream=minted,
    )
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    resolved = vidara.vidara("https://vidara.to/v/abc")

    assert resolved["link"] == minted
    assert session.posted == [("https://vidara.to/api/stream", "abc")]

def test_resolve_keeps_the_gateway_url_when_minting_here_fails(vidara, monkeypatch):
    """A host that cannot reach Vidara directly is the case the gateway exists
    for; a failed mint must not take the resolved link down with it."""
    session = _MintSession(
        _Resp(200, _ok_body(page_url="https://vidara.to/v/abc")),
        error=OSError("no route to host"),
    )
    monkeypatch.setattr(vidara, "Session", lambda: session)

    _, link, _ = vidara.vidara_resolve("https://vidara.to/abc")

    assert link == "https://cdn.test/master.m3u8"

def test_mint_refuses_a_host_this_module_does_not_serve(vidara):
    """The page URL can come from a message, so the mint only posts to a host
    this module already recognises."""
    session = _MintSession(stream="https://evil.test/master.m3u8")

    assert vidara.vidara_mint_stream(session, "https://evil.test/v/abc") == ""
    assert session.posted == []
