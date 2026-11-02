"""x.com / instagram.com / tiktok.com post resolver, via the gateway.

The gateway answers ``/api/v1/scrape/{x,instagram,tiktok}`` with one post and
the media it carries: X and Instagram both in ``media`` (Instagram flattens a
carousel into that list), TikTok as one ``video`` or a slideshow ``images``.
One module because everything but that last shape is shared -- the endpoint,
the retry, the file naming.

Every URL they answer with is a signed CDN link that expires, so the post is
resolved once and its media handed to aria2 as a ``contents`` list, fetched
immediately. TikTok's CDN additionally demands the ``Referer`` and
``tt_chain_token`` cookie the gateway probed with, which travels as the
descriptor's ``header``.
"""

from os import path as ospath
from time import sleep
from urllib.parse import urlparse

from requests import Session

from .._common import (
    LOGGER,
    MEDIA_EXTS,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    header_lines,
    safe_stem,
    unique_stem,
)
from ..registry import register

X_HOSTS = ("x.com", "twitter.com")
INSTAGRAM_HOSTS = ("instagram.com",)
TIKTOK_HOSTS = ("tiktok.com",)

ATTEMPTS = 3
MAX_RETRY_DELAY = 30
# "Come back later" statuses. A rate limit mistaken for a dead post costs a
# link that was fine, which is why every gateway host retries these.
RETRY_STATUSES = (408, 425, 429)

_PHOTO_EXTS = (".jpg", ".jpeg", ".webp", ".png", ".heic")

def _is_host(url: str, hosts: tuple[str, ...]) -> bool:
    """Match the host itself or a subdomain of it.

    Exact-or-suffix rather than substring on purpose: ``"x.com" in domain``
    would also match ``netflix.com``, and a post handler that swallows the
    wrong host fails in a way nobody would look for here.
    """
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == host or domain.endswith(f".{host}") for host in hosts)

def is_x_link(url: str) -> bool:
    return _is_host(url, X_HOSTS)

def is_instagram_link(url: str) -> bool:
    return _is_host(url, INSTAGRAM_HOSTS)

def is_tiktok_link(url: str) -> bool:
    return _is_host(url, TIKTOK_HOSTS)

def _read(endpoint: str, url: str) -> dict:
    """Ask the gateway for one post, retrying what is worth retrying.

    A hiccup or a rate limit is worth another attempt; a private or deleted
    post is not. Only a retry bypasses the gateway's stored answer, which is
    what the attempt that just failed was served from.
    """
    reason = "unknown error"
    for attempt in range(1, ATTEMPTS + 1):
        params = {"q": url}
        if attempt > 1:
            params["nocache"] = "1"
        try:
            with Session() as session:
                resp = session.get(
                    gateway_url(endpoint),
                    params=params,
                    headers=gateway_headers(),
                    timeout=60,
                )
        except Exception as exc:
            reason, retryable = exc.__class__.__name__, True
        else:
            retryable = resp.status_code >= 500 or resp.status_code in RETRY_STATUSES
            try:
                body = resp.json()
            except Exception:
                reason = f"HTTP {resp.status_code} (non-JSON response)"
            else:
                if body.get("success"):
                    return body.get("data") or {}
                reason = body.get("error") or f"HTTP {resp.status_code}"
        if not retryable or attempt == ATTEMPTS:
            break
        delay = min(2**attempt, MAX_RETRY_DELAY)
        LOGGER.info(
            f"{endpoint}: {reason}, retrying in {delay}s [{attempt}/{ATTEMPTS}]"
        )
        sleep(delay)
    raise DirectDownloadLinkException(f"ERROR: {reason}")

def _extension(url: str, kind: str) -> str:
    """The container off the URL's path, or what the kind usually arrives as.

    The CDN URLs carry the suffix, but the query that follows them is signed
    and can be long, which is why the path -- not the whole URL -- is split.
    """
    suffix = ospath.splitext(urlparse(url).path)[1].lower()
    if suffix in MEDIA_EXTS or suffix in _PHOTO_EXTS:
        return suffix
    return ".mp4" if kind == "video" else ".jpg"

def _media_entries(media: list, stem: str) -> list[dict]:
    """``media`` items as ``contents`` entries, one file each."""
    entries = []
    taken: set[str] = set()
    for index, item in enumerate(media):
        url = str((item or {}).get("url") or "").strip()
        if not url.startswith("http"):
            continue
        name = unique_stem(stem, taken, str(index))
        entries.append(
            {
                "path": "",
                "filename": f"{name}{_extension(url, item.get('kind') or '')}",
                "url": url,
            }
        )
    return entries

def _descriptor(entries: list[dict], title: str, headers: dict | None) -> dict:
    return {
        "contents": entries,
        "title": title,
        "total_size": 0,
        "header": header_lines(headers or {}),
    }

@register(predicate=is_x_link, order=92)
def x_post(url: str) -> dict:
    """Resolve one tweet to its photos and MP4s."""
    data = _read("/api/v1/scrape/x", url)
    stem = safe_stem(data.get("text"), f"x {data.get('id') or 'post'}")
    entries = _media_entries(data.get("media") or [], stem)
    if not entries:
        raise DirectDownloadLinkException("ERROR: this post carries no photo or video")
    return _descriptor(entries, stem, (data.get("download") or {}).get("headers"))

@register(predicate=is_instagram_link, order=93)
def instagram_post(url: str) -> dict:
    """Resolve one post, reel or IGTV page to its photo(s) and video(s)."""
    data = _read("/api/v1/scrape/instagram", url)
    fallback = f"instagram {data.get('shortcode') or 'post'}"
    stem = safe_stem(data.get("caption"), fallback)
    entries = _media_entries(data.get("media") or [], stem)
    if not entries:
        raise DirectDownloadLinkException("ERROR: this post carries no media")
    return _descriptor(entries, stem, (data.get("download") or {}).get("headers"))

@register(predicate=is_tiktok_link, order=94)
def tiktok_post(url: str) -> dict:
    """Resolve one TikTok post: the no-watermark MP4, or a slideshow's frames."""
    data = _read("/api/v1/scrape/tiktok", url)
    stem = safe_stem(data.get("desc"), f"tiktok {data.get('id') or 'post'}")

    video = data.get("video") or {}
    media = []
    # ``play_url`` is the rendition the web player streams, without the
    # watermark the Download button adds; the watermarked one is only a fallback.
    if play := video.get("play_url") or video.get("download_url"):
        media.append({"kind": "video", "url": play})
    for image in data.get("images") or []:
        media.append({"kind": "image", "url": (image or {}).get("url")})

    entries = _media_entries(media, stem)
    if not entries:
        raise DirectDownloadLinkException("ERROR: this post carries no media")
    return _descriptor(entries, stem, data.get("headers"))
