"""Erome album/profile handler.

The gateway reads an album or a profile and answers with every media entry
already resolved: an erome CDN link is not signed, so nothing has to be minted
per file the way ShyAV's or Bunkr's links are, and the whole listing is handed
to the direct downloader at once. Two shapes need care beyond the request -- an
album mixes images with videos, and a profile is a list of albums whose files
would overwrite each other in a flat folder, so each entry keeps its own album
id as its ``path``.
"""
from __future__ import annotations

from os import path as ospath
from urllib.parse import urlparse

from requests import Session

from .._common import (
    LOGGER,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    safe_name,
    safe_stem,
    unique_stem,
)
from ..registry import register

EROME_PATH = "/api/v1/scrape/erome"
EROME_DOMAINS = ("erome.com",)


def is_erome_link(url: str) -> bool:
    """Match the host itself or a subdomain of it, so a lookalike like
    noterome.com is not mistaken for the real thing."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == d or domain.endswith(f".{d}") for d in EROME_DOMAINS)


def erome_read(url: str) -> dict:
    """Read the gateway endpoint and classify what came back.

    A gateway hiccup and a removed album both land here as one failure: unlike
    a bulk of signed links, nothing downstream retries an album resolve, and
    the reason is what the user needs to see.
    """
    try:
        with Session() as session:
            resp = session.get(
                gateway_url(EROME_PATH),
                params={"q": url},
                headers=gateway_headers(),
                timeout=120,
            )
    except Exception as exc:
        raise DirectDownloadLinkException(
            f"ERROR: Erome API request failed: {exc}"
        ) from exc
    try:
        response = resp.json()
    except Exception as exc:
        raise DirectDownloadLinkException(
            f"ERROR: Erome API returned HTTP {resp.status_code} without JSON"
        ) from exc
    if not response.get("success"):
        raise DirectDownloadLinkException(
            f"ERROR: Erome resolve failed: "
            f"{response.get('error') or f'HTTP {resp.status_code}'}"
        )
    return response


def erome_entries(media: list, grouped: bool) -> list[dict]:
    """The media of a response as ``contents`` entries.

    *grouped* is a profile, whose entries are filed under their album id: two
    albums routinely reuse a file name, and a flat listing would fetch one over
    the other. Names are deduplicated per folder, not across the listing: the
    same name in two albums is two files, and only a repeat inside one folder
    is a collision.
    Every entry carries a ``Referer`` header: erome's video CDN rejects
    requests without it (403), images work without it but the header is safe
    for both.
    """
    taken: dict[str, set[str]] = {}
    contents: list[dict] = []
    for index, item in enumerate(media, 1):
        if not isinstance(item, dict):
            continue
        link = item.get("url") or ""
        if not link.startswith("http"):
            continue
        folder = str(item.get("album_id") or "") if grouped else ""
        name = item.get("filename") or ospath.basename(urlparse(link).path)
        name = safe_name(name, f"erome {index}")
        stem, ext = ospath.splitext(name)
        filename = f"{unique_stem(stem, taken.setdefault(folder, set()))}{ext}"
        contents.append(
            {
                "path": folder,
                "filename": filename,
                "url": link,
                "headers": ["Referer: https://www.erome.com/"],
            }
        )
    return contents


@register(predicate=is_erome_link, order=45)
def erome(url: str) -> dict:
    """Erome handler: an album is one folder, a profile is one folder per album."""
    response = erome_read(url)
    grouped = response.get("kind") == "profile"
    contents = erome_entries(response.get("media") or [], grouped)
    if not contents:
        raise DirectDownloadLinkException("ERROR: no media in this Erome album")
    album = response.get("album") or {}
    fallback = response.get("profile") or "erome"
    title = safe_stem(album.get("title") or fallback, fallback)
    LOGGER.info(f"Erome: {title} lists {len(contents)} media")
    return {"contents": contents, "title": title, "total_size": 0}
