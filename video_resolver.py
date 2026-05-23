"""Match a Legistar event to a NYC Council YouTube /streams video.

The NYC Council livestreams most committee hearings on YouTube under
`@NYCCouncil/streams`. Their auto-generated captions are noticeably
cleaner than Viebit's CEA-608 dedupe, so when a YouTube counterpart
exists we prefer it as the transcript source.

Single entry point:

    find_youtube_match(event_date_iso, committee_name, topic=None) -> dict | None

Returns ``{"url", "title", "score", "upload_date"}`` for the best
plausible match, or ``None`` if no candidate clears the score threshold.
The caller is then free to fall back to the Viebit URL that
``council_scraper`` already provides.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import yt_dlp

logger = logging.getLogger(__name__)

CHANNEL_STREAMS_URL = "https://www.youtube.com/@NYCCouncil/streams"

# Tokens that carry no discriminative power for matching a hearing —
# they appear in nearly every committee name or video title.
STOP_TOKENS = {
    "the", "and", "of", "on", "in", "a", "to", "for", "with", "from",
    "committee", "subcommittee", "council", "nyc", "new", "york", "city",
    "hearing", "oversight", "live", "watch", "join", "joins",
}

# Channel listings include press conferences and "Speaker Menin Joins…"
# segments. A committee hearing always runs at least this long.
MIN_DURATION_MINUTES = 30


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, drop stop tokens. Tokens of length >= 3 only."""
    if not text:
        return set()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return {t for t in cleaned.split() if len(t) >= 3 and t not in STOP_TOKENS}


def _clean_title_for_scoring(title: str) -> str:
    """Strip the leading red-circle + LIVE marker that prefixes almost every entry."""
    title = re.sub(r"^[\U0001F300-\U0001FAFF☀-➿︀-️‍]+\s*", "", title)
    title = re.sub(r"^(LIVE|REPLAY|WATCH LIVE|FULL HEARING)[:\s\-–—|]*", "", title, flags=re.IGNORECASE)
    return title.strip()


def _score(query_tokens: set[str], title_tokens: set[str]) -> float:
    """Jaccard-style overlap of query tokens against title tokens.

    Score = |intersection| / |query|. Anchored on the query side so adding
    irrelevant title tokens (boilerplate verbs like "addressing") doesn't
    drag the score down.
    """
    if not query_tokens:
        return 0.0
    return len(query_tokens & title_tokens) / len(query_tokens)


def _fetch_recent_streams(fetch_limit: int) -> list[dict]:
    """Non-flat extract of the most recent /streams entries.

    Non-flat is required because YouTube's flat metadata path drops
    `upload_date` for channel tabs, which we need for the date filter.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "playlistend": fetch_limit,
        "skip_download": True,
    }
    logger.info(f"Fetching {fetch_limit} recent /streams entries from {CHANNEL_STREAMS_URL}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(CHANNEL_STREAMS_URL, download=False)
    return list(info.get("entries") or [])


def find_youtube_match(
    event_date_iso: str,
    committee_name: str,
    topic: str | None = None,
    *,
    days_window: int = 2,
    fetch_limit: int = 30,
    score_threshold: float = 0.25,
) -> dict | None:
    """Find a /streams video plausibly matching the given Legistar event.

    Args:
        event_date_iso: "YYYY-MM-DD" date the hearing was held.
        committee_name: primary committee (joint hearings: caller passes the lead).
        topic: optional hearing topic from the agenda — boosts matches when
            the YouTube title is topic-led (e.g. "Oversight Hearing on Fair Fares"
            for a Transportation committee event).
        days_window: ± window in days around `event_date_iso` to consider.
        fetch_limit: how many recent /streams entries to scan.
        score_threshold: minimum token-overlap score to accept a match.

    Returns:
        ``{"url", "title", "score", "upload_date"}`` for the highest-scoring
        candidate, or ``None`` if nothing clears the threshold.
    """
    try:
        target_date = datetime.strptime(event_date_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        logger.warning(f"find_youtube_match: invalid event_date_iso {event_date_iso!r}")
        return None

    try:
        entries = _fetch_recent_streams(fetch_limit)
    except Exception as e:
        logger.warning(f"find_youtube_match: /streams fetch failed: {e}")
        return None

    query_tokens = _tokenize(committee_name) | _tokenize(topic or "")
    if not query_tokens:
        logger.warning("find_youtube_match: no usable query tokens after filtering")
        return None

    candidates = []
    for e in entries:
        upload_date_str = e.get("upload_date")
        if not upload_date_str:
            continue
        try:
            upload_date = datetime.strptime(upload_date_str, "%Y%m%d").date()
        except ValueError:
            continue
        if abs((upload_date - target_date).days) > days_window:
            continue
        duration_s = e.get("duration") or 0
        if duration_s < MIN_DURATION_MINUTES * 60:
            continue

        title = e.get("title") or ""
        title_tokens = _tokenize(_clean_title_for_scoring(title))
        score = _score(query_tokens, title_tokens)
        candidates.append({
            "id": e.get("id"),
            "title": title,
            "upload_date": upload_date_str,
            "duration_s": duration_s,
            "score": score,
        })

    if not candidates:
        logger.info(
            f"find_youtube_match: no /streams videos in ±{days_window}d of "
            f"{event_date_iso} with duration >= {MIN_DURATION_MINUTES}min"
        )
        return None

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]
    logger.info(
        f"find_youtube_match: best candidate score={best['score']:.2f} "
        f"({best['upload_date']}, {best['duration_s']//60}min): {best['title']!r}"
    )
    if best["score"] < score_threshold:
        logger.info(
            f"find_youtube_match: best score {best['score']:.2f} below "
            f"threshold {score_threshold:.2f} — no match"
        )
        return None
    return {
        "url": f"https://www.youtube.com/watch?v={best['id']}",
        "title": best["title"],
        "score": best["score"],
        "upload_date": best["upload_date"],
    }
