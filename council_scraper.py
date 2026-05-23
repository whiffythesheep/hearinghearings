"""Scrape the NYC Council Legistar website (Calendar + MeetingDetail).

The public website (council.nyc.gov / legistar.council.nyc.gov) exposes
agenda PDFs, video links, and canonical meeting URLs without
authentication. This module wraps that scraping so the summarizer can
auto-fill the manual inputs from a single Legistar URL.

Single entry point:

    scrape_event(legistar_url, *, agenda_cache_dir) -> dict

Returned dict keys:
    event_id, event_guid, council_url, agenda_url, agenda_path,
    video_url (None if not yet archived).
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse as up
from pathlib import Path

import requests

LEGISTAR_HOST = "https://legistar.council.nyc.gov"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}

logger = logging.getLogger(__name__)


def parse_legistar_url(url: str) -> tuple[str, str]:
    """Extract (event_id, event_guid) from a MeetingDetail URL."""
    m = re.search(
        r"MeetingDetail\.aspx\?ID=(\d+)&[^\"\s]*GUID=([A-F0-9-]+)",
        url,
        re.IGNORECASE,
    )
    if not m:
        raise ValueError(f"Not a Legistar MeetingDetail URL: {url}")
    return m.group(1), m.group(2)


def _decode_video_url(html: str) -> str | None:
    """Decode the base64 video URL out of the page's onclick handler.

    The handler is rendered as `OpenTelerikWindow(&#39;Video.aspx?Mode=Auto&amp;URL=<base64>&#39;,...)`,
    with HTML-escaped quotes and ampersands. A loose match between
    `URL=` and the next non-base64 character is reliable here because
    the only consumer of this string is the player popup.
    """
    m = re.search(r"OpenTelerikWindow\(.+?URL=([A-Za-z0-9%+/=]+)", html)
    if not m:
        return None
    encoded = up.unquote(m.group(1))
    try:
        return base64.b64decode(encoded).decode("ascii")
    except Exception as e:
        logger.warning("Failed to base64-decode video URL: %s", e)
        return None


def _download_agenda(event_id: str, event_guid: str, dest_dir: Path) -> Path:
    """Download and cache the agenda PDF for an event."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{event_id}-agenda.pdf"
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("Agenda already cached: %s", dest.name)
        return dest
    url = f"{LEGISTAR_HOST}/View.ashx?M=A&ID={event_id}&GUID={event_guid}"
    logger.info("Downloading agenda from %s", url)
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def scrape_event(legistar_url: str, *, agenda_cache_dir: Path) -> dict:
    """Scrape a MeetingDetail page and return enriched event metadata."""
    event_id, event_guid = parse_legistar_url(legistar_url)
    canonical_url = (
        f"{LEGISTAR_HOST}/MeetingDetail.aspx"
        f"?ID={event_id}&GUID={event_guid}&Search="
    )
    logger.info("Scraping Legistar event %s", event_id)
    r = requests.get(canonical_url, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text

    video_url = _decode_video_url(html)
    agenda_path = _download_agenda(event_id, event_guid, agenda_cache_dir)

    return {
        "event_id": event_id,
        "event_guid": event_guid,
        "council_url": canonical_url,
        "agenda_url": (
            f"{LEGISTAR_HOST}/View.ashx?M=A&ID={event_id}&GUID={event_guid}"
        ),
        "agenda_path": agenda_path,
        "video_url": video_url,
    }
