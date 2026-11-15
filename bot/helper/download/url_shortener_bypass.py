"""URL shortener bypassers.

Resolves a shortened URL back to its underlying target URL through the
gateway's unified ``/api/v2/bypass``. The bot keeps no list of shortener
domains and does no validation of its own: that endpoint picks the resolver
from the URL and answers 400 for anything it does not know, and that answer is
what sends a link to the regular host handlers instead. The caller in
direct_link_generator.py re-dispatches the resolved URL through those handlers.
"""

from random import uniform
from re import search
from time import sleep

from requests import Session

from ..net.gateway import gateway_headers, gateway_url
from ..util.exceptions import DirectDownloadLinkException

_BYPASS_PATH = "/api/v2/bypass"

_GATEWAY_ATTEMPTS = 3
_GATEWAY_TIMEOUT = 60
_GATEWAY_MAX_RETRY_DELAY = 30
# 500 is deliberately absent: the gateway answers a dead shortlink with
# 500 + "Content Not Found", and retrying that only triples the wait.
_RETRY_STATUSES = (408, 425, 429, 502, 503, 504)
# The gateway's answer for a link it has no resolver for. The bot holds no list
# of its own, so this status is what routes the link to the host handlers.
_UNSUPPORTED_STATUS = 400
_TRANSIENT_HINTS = (
    "rate limit",
    "rate-limit",
    "too many request",
    "slow down",
    "timeout",
    "timed out",
    "temporarily",
    "try again",
)
_URL_IN_TEXT = r"https?://[^\s\"'<>]+"
# Some gateway errors quote the whole fetched page back — ouo does that when the
# CSRF token is missing — so clamp what ends up in the user's chat.
_MAX_REASON = 300


def _transient(reason):
    """The gateway also reports a rate limit as HTTP 200 + success: false."""
    text = str(reason).lower()
    return any(hint in text for hint in _TRANSIENT_HINTS)


def _clip(reason):
    """A one-line, chat-sized version of a gateway error."""
    text = " ".join(str(reason).split())
    return text if len(text) <= _MAX_REASON else f"{text[:_MAX_REASON]}…"


def _retry_delay(attempt, retry_after=None):
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: a bulk resolves in lockstep, so a fixed sleep only
    reschedules the same burst.
    """
    if retry_after:
        try:
            delay = min(float(retry_after), _GATEWAY_MAX_RETRY_DELAY)
            return delay + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2 ** (attempt - 1), _GATEWAY_MAX_RETRY_DELAY) + uniform(0, 1.5)


def _target(payload):
    """The destination URL carried by a BypassResponse.

    Paste-type Linkvertise links come back as the paste body instead of a URL,
    so pull the first link out of it when that happens.
    """
    value = (payload.get("url") or "").strip()
    if value.startswith("http"):
        return value
    if value and (m := search(_URL_IN_TEXT, value)):
        return m.group(0)
    return ""


def _gateway_request(session, api, headers, link):
    """Return (target, reason, retryable, retry_after, known).

    ``known`` is False for the gateway's 400: it does not recognise the link as
    one of its shortlinks, which is the only place that is decided. A gateway
    hiccup or a rate limit is worth another attempt; a shortlink whose content
    is gone is not — the gateway reports that as 500, so the retry decision
    leans on the error text rather than the status alone.
    """
    try:
        resp = session.get(
            api, params={"url": link}, headers=headers, timeout=_GATEWAY_TIMEOUT
        )
    except Exception as e:
        return None, e.__class__.__name__, True, None, True

    retry_after = resp.headers.get("Retry-After")
    retryable = resp.status_code in _RETRY_STATUSES
    known = resp.status_code != _UNSUPPORTED_STATUS

    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        reason = f"HTTP {resp.status_code} (non-JSON response)"
        return None, reason, retryable, retry_after, known

    if not payload.get("success"):
        reason = payload.get("error") or f"HTTP {resp.status_code}"
        retryable = retryable or (known and _transient(reason))
        return None, reason, retryable, retry_after, known

    if target := _target(payload):
        return target, None, False, None, True

    body = (payload.get("url") or "").strip()
    reason = "no target URL in response"
    if body:
        reason += f" (paste content: {body[:200]})"
    return None, reason, False, None, True


def bypass_shortener(link: str) -> str | None:
    """Resolve a link through the gateway's unified /api/v2/bypass.

    Returns the target URL, or None when the gateway says the link is not a
    shortlink it knows — the caller then falls through to the host handlers.
    """
    api = gateway_url(_BYPASS_PATH)
    headers = gateway_headers()
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, _GATEWAY_ATTEMPTS + 1):
            target, reason, retryable, retry_after, known = _gateway_request(
                session, api, headers, link
            )
            if target:
                return target
            if not known:
                return None
            if not retryable or attempt == _GATEWAY_ATTEMPTS:
                break
            sleep(_retry_delay(attempt, retry_after))

    raise DirectDownloadLinkException(f"ERROR: Bypass failed: {_clip(reason)}")
