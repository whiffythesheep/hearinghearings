"""Scrape the NYC Council Legistar website (Calendar + MeetingDetail).

The public website (council.nyc.gov / legistar.council.nyc.gov) exposes
agenda PDFs, video links, and canonical meeting URLs without
authentication. This module wraps that scraping so the summarizer can
auto-fill the manual inputs from a single Legistar URL.

Entry points:

    scrape_event(legistar_url, *, agenda_cache_dir) -> dict
        Per-event MeetingDetail scrape (used by the summarizer's
        --legistar-url flow).

    list_calendar_events() -> list[dict]
        Trigger source for the discovery poller — parses the ~100
        most-recent rows of Calendar.aspx, including body name, date,
        agenda/video availability, the location-row VOTE marker, and
        (when available) the decoded Viebit URL straight from the row's
        onclick handler.

    fetch_viebit_duration_seconds(viebit_url) -> int | None
        Reads the final caption cue's end timestamp from a Viebit watch
        page without parsing the whole VTT. Used by the discovery
        filter to drop short procedural hearings before the expensive
        pipeline run.
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse as up
from datetime import datetime
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


# --- Calendar.aspx scraping ---------------------------------------------

CALENDAR_URL = f"{LEGISTAR_HOST}/Calendar.aspx"

# Each <tr class="rgRow" | "rgAltRow"> wraps a single meeting. The row's
# ASP.NET ID ends with `__<NN>`, and every <a>/<td> inside reuses an
# adjacent `ctlNN` (typically `__NN` * 2 + 4). We parse rows individually
# so per-row classes like `videoFileNotAvailableLink` stay attached to
# their event.
_ROW_RE = re.compile(
    r'<tr class="(?:rgRow|rgAltRow)"[^>]*id="ctl00_ContentPlaceHolder1_'
    r'gridCalendar_ctl00__\d+"[^>]*>(.*?)</tr>',
    re.DOTALL,
)


def _strip_html(s: str) -> str:
    """Drop tags + decode common entities so 'rgSorted' cells yield plain text."""
    s = re.sub(r"<[^>]+>", "", s)
    s = (
        s.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", s).strip()


def _parse_calendar_row(row_html: str) -> dict | None:
    """Pull the bits of one rgRow we care about. Returns None if no detail link."""
    detail = re.search(
        r'href="MeetingDetail\.aspx\?ID=(\d+)&amp;GUID=([A-F0-9-]+)',
        row_html,
        re.IGNORECASE,
    )
    if not detail:
        return None
    event_id, event_guid = detail.group(1), detail.group(2)

    body_match = re.search(r'_hypBody"[^>]*>([^<]+)</a>', row_html)
    body_name = _strip_html(body_match.group(1)) if body_match else ""

    # First "rgSorted" td in each row is the meeting date (M/D/YYYY).
    date_match = re.search(
        r'<td class="rgSorted">([^<]+)</td>', row_html, re.IGNORECASE,
    )
    event_date_iso = ""
    if date_match:
        try:
            event_date_iso = datetime.strptime(
                date_match.group(1).strip(), "%m/%d/%Y"
            ).date().isoformat()
        except ValueError:
            event_date_iso = ""

    # The "<em>...</em>" inside the location cell flags procedural rows
    # (e.g. "VOTE*" for vote-only sessions we want to skip).
    location_em = ""
    em_match = re.search(r"<em>([^<]*)</em>", row_html)
    if em_match:
        location_em = _strip_html(em_match.group(1))

    # Topic ("Description" column) sits in its own <td> between the
    # location cell and the meeting-details link. The location cell
    # always ends in <br /><em>...</em></td>, so we anchor there to
    # avoid getting confused by earlier <td> boundaries in the row.
    topic = ""
    topic_match = re.search(
        r"<br ?/?>\s*<em>[^<]*</em>\s*</td>\s*<td>([^<]*)</td>",
        row_html,
    )
    if topic_match:
        topic = _strip_html(topic_match.group(1))

    has_agenda = bool(
        re.search(r'_hypAgenda"[^>]*href="View\.ashx\?M=A', row_html)
    )

    # Video is available iff hypVideo carries an onclick handler with the
    # base64 URL. When unavailable the same <a> has class
    # "videoFileNotAvailableLink" and "Not available" text.
    video_url = None
    video_match = re.search(
        r'_hypVideo"[^>]*onclick="OpenTelerikWindow\([^"]*?URL='
        r'([A-Za-z0-9%+/=]+)',
        row_html,
    )
    if video_match:
        try:
            encoded = up.unquote(video_match.group(1))
            video_url = base64.b64decode(encoded).decode("ascii")
        except Exception as e:
            logger.warning(
                "Failed to decode video URL for event %s: %s", event_id, e,
            )

    council_url = (
        f"{LEGISTAR_HOST}/MeetingDetail.aspx"
        f"?ID={event_id}&GUID={event_guid}&Search="
    )

    return {
        "event_id": event_id,
        "event_guid": event_guid,
        "body_name": body_name,
        "event_date": event_date_iso,
        "location_em": location_em,
        "topic": topic,
        "has_agenda": has_agenda,
        "has_video": video_url is not None,
        "video_url": video_url,
        "council_url": council_url,
    }


def list_calendar_events() -> list[dict]:
    """Return all rows visible on the default Calendar.aspx view (~100 rows).

    Each dict carries the row's body name, ISO date, location <em>
    marker (e.g. "VOTE*"), topic blurb, agenda/video availability, the
    decoded Viebit URL when present, and the canonical MeetingDetail
    URL. Idempotency, skip filtering, and pipeline triggering all run
    off this single payload.
    """
    logger.info("Fetching Calendar.aspx")
    r = requests.get(CALENDAR_URL, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    rows = _ROW_RE.findall(r.text)
    events: list[dict] = []
    for row_html in rows:
        parsed = _parse_calendar_row(row_html)
        if parsed:
            events.append(parsed)
    logger.info("Parsed %d calendar rows", len(events))
    return events


# --- Viebit duration probe ---------------------------------------------

VIEBIT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)


def fetch_viebit_duration_seconds(viebit_url: str) -> int | None:
    """Return the hearing duration in seconds, derived from the VTT's final cue.

    Reuses the same caption-URL regex `summarize_council_meeting.py`
    uses, so works for both `/watch?hash=...` and `/vod/?v=...` shapes
    of viebit URL. Scans the cue file from the end to find the last
    `HH:MM:SS.mmm --> HH:MM:SS.mmm` line and returns the right-hand
    end-timestamp in whole seconds. Returns None on any failure — the
    caller treats that as "unknown duration, don't filter".
    """
    try:
        r = requests.get(viebit_url, headers=BROWSER_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logger.warning("Viebit page fetch failed for %s: %s", viebit_url, e)
        return None
    cap_match = re.search(
        r'"src"\s*:\s*"(https://[^"]+\.vtt[^"]*)"', r.text,
    )
    if not cap_match:
        logger.info("No caption URL on Viebit page for %s", viebit_url)
        return None
    caption_url = cap_match.group(1)
    try:
        v = requests.get(caption_url, headers=BROWSER_HEADERS, timeout=60)
        v.raise_for_status()
    except Exception as e:
        logger.warning("VTT fetch failed for %s: %s", caption_url, e)
        return None
    matches = list(VIEBIT_TIME_RE.finditer(v.text))
    if not matches:
        return None
    last = matches[-1]
    h, m, s = int(last.group(5)), int(last.group(6)), int(last.group(7))
    return h * 3600 + m * 60 + s
