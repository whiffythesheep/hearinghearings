"""
Summarize NYC Council meetings from YouTube video + PDF agenda.

Fetches YouTube's auto-generated transcript, uses Claude to identify speakers
and segment into turns, cleans the transcript, generates a summary, and
publishes to hearinghearings.nyc.

Usage:
    python summarize_council_meeting.py <youtube_url> <agenda.pdf>
    python summarize_council_meeting.py <youtube_url> <agenda.pdf> --skip-fetch
    python summarize_council_meeting.py <youtube_url> <agenda.pdf> --skip-clean
    python summarize_council_meeting.py --transcript-json <cached.json> <agenda.pdf>
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import anthropic
import pdfplumber
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            Path(__file__).parent / "summarize.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

# --- Configuration ---
SCRIPT_DIR = Path(__file__).parent.resolve()
INPUT_DIR = SCRIPT_DIR / "Input"
WEBSITE_CONTENT_DIR = SCRIPT_DIR / "content"
DATE_PREFIX = datetime.now().strftime("%y.%m")
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
# Max characters to send in a single Anthropic API call (~4 chars per token).
# Claude Sonnet has 200k context; we leave room for the prompt and response.
MAX_TRANSCRIPT_CHARS = 100_000
MAX_SEGMENT_CHARS = 30_000  # Max characters per speaker segmentation API call


def clean_youtube_title(title):
    """Strip common YouTube livestream prefixes/emojis from titles."""
    # Remove leading emoji characters (🔴, 🔵, etc.)
    title = re.sub(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE00-\uFE0F\u200D]+\s*", "", title)
    # Remove common livestream prefixes
    title = re.sub(r"^(LIVE|REPLAY|WATCH LIVE|FULL HEARING)[:\s\-–—|]*", "", title, flags=re.IGNORECASE)
    return title.strip()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Summarize a NYC Council meeting from YouTube + PDF agenda."
    )
    parser.add_argument("youtube_url", nargs="?", default=None,
                        help="YouTube URL of the meeting (optional if --transcript-json is given)")
    parser.add_argument("agenda_pdf", nargs="?", default=None,
                        help="Path to the PDF agenda file")
    parser.add_argument(
        "--transcript-json", type=str, default=None,
        help="Path to cached transcript JSON file (skips fetch and URL requirement)"
    )
    parser.add_argument(
        "--skip-fetch", action="store_true",
        help="Skip YouTube transcript fetch (use cached JSON in Input/)"
    )
    parser.add_argument(
        "--skip-clean", action="store_true",
        help="Skip transcript cleanup (use raw transcript text)"
    )
    parser.add_argument(
        "--title", type=str, default=None,
        help="Meeting name (e.g. 'Oversight – Shared Housing in NYC'). "
             "Combined with committee name from agenda to form full title."
    )
    parser.add_argument(
        "--council-url", type=str, default=None,
        help="Legistar MeetingDetail URL for the hearing "
             "(e.g. 'https://legistar.council.nyc.gov/MeetingDetail.aspx?ID=...&GUID=...&Search=')."
    )
    parser.add_argument(
        "--skip-summary", type=str, default=None, metavar="SLUG",
        help="Skip summary generation and reuse existing published page. "
             "Only updates the transcript section. Pass the existing slug "
             "(e.g. 'committee-on-housing-and-buildings-oversight-shared-housing-in-nyc')."
    )
    parser.add_argument(
        "--no-deploy", action="store_true",
        help="Write the markdown but skip site build, git commit, and push. "
             "Use when batch-processing multiple meetings to defer build/push."
    )
    return parser.parse_args()


def estimate_costs_and_confirm(url):
    """Show estimated API costs and ask user to confirm before proceeding.

    Returns the video info dict (with duration and title) for reuse.
    """
    logger.info("Fetching video info...")
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    duration_s = info.get("duration", 0)
    hours = duration_s / 3600
    mins = duration_s % 3600 // 60

    # Anthropic: ~25,000 chars/hour of transcript, ~4 chars/token
    est_chars = hours * 25_000
    # Speaker segmentation pass (~full transcript input)
    segment_tokens = est_chars / 4
    segment_cost = (segment_tokens / 1_000_000 * 3) + (5_000 / 1_000_000 * 15)
    # Summarization pass
    summary_tokens = (est_chars + 2_000) / 4
    if est_chars > MAX_TRANSCRIPT_CHARS:
        n_chunks = (est_chars // MAX_TRANSCRIPT_CHARS) + 1
        summary_tokens = summary_tokens * n_chunks + 10_000
    summary_cost = (summary_tokens / 1_000_000 * 3) + (2_000 / 1_000_000 * 15)
    # Cleaning pass (~full transcript)
    clean_cost = (est_chars / 4 / 1_000_000 * 3) + (est_chars / 4 / 1_000_000 * 15)

    anthropic_cost = segment_cost + summary_cost + clean_cost

    h_display = int(hours)
    m_display = int(mins)

    logger.info(f"Video duration: {h_display}h {m_display:02d}m")
    logger.info(f"Estimated Anthropic cost: ~${anthropic_cost:.2f}")

    response = input("Proceed? [y/N] ").strip()
    if response.lower() != "y":
        logger.info("Aborted.")
        sys.exit(0)

    return info


def extract_video_id(url):
    """Extract YouTube video ID from a URL."""
    patterns = [
        r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    logger.error(f"Could not extract video ID from URL: {url}")
    sys.exit(1)


def fetch_youtube_transcript(url, video_info, skip=False):
    """Fetch auto-generated transcript from YouTube.

    Returns (segments, json_path) where segments is a list of dicts with
    'text', 'start_ms', 'end_ms' keys, and json_path is the cache file path.
    """
    title = video_info.get("title", "council_meeting")
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)[:80].strip()
    json_path = INPUT_DIR / f"{DATE_PREFIX}- {safe_title}.json"

    # Check cache
    if skip or json_path.exists():
        if json_path.exists():
            logger.info(f"Transcript already cached: {json_path.name}")
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "raw_segments" in data:
                return data["raw_segments"], json_path
        if skip:
            logger.error("--skip-fetch specified but no cached transcript found.")
            sys.exit(1)

    video_id = extract_video_id(url)
    logger.info(f"Fetching YouTube transcript for video {video_id}...")

    try:
        api = YouTubeTranscriptApi()
        raw = api.fetch(video_id, languages=["en"])
    except Exception as e:
        logger.error(f"Could not fetch YouTube transcript: {e}")
        logger.error("The video may not have auto-generated captions yet. Try again later.")
        sys.exit(1)

    # Convert to consistent format with millisecond timestamps
    segments = []
    for seg in raw:
        start_ms = int(seg.start * 1000)
        end_ms = int((seg.start + seg.duration) * 1000)
        segments.append({
            "text": seg.text,
            "start_ms": start_ms,
            "end_ms": end_ms,
        })

    logger.info(f"Fetched {len(segments)} transcript segments.")

    # Cache
    cache_obj = {
        "title": title,
        "video_id": video_id,
        "raw_segments": segments,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cache_obj, f, indent=2, ensure_ascii=False)
    logger.info(f"Transcript cached: {json_path.name}")

    return segments, json_path


def parse_agenda(pdf_path):
    """Extract agenda text from PDF."""
    logger.info(f"Parsing agenda: {Path(pdf_path).name}")
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    agenda_text = "\n".join(text_parts)
    if not agenda_text.strip():
        logger.warning(" No text extracted from agenda PDF.")
    return agenda_text


def extract_agenda_metadata(agenda_text, client):
    """Extract committee name and meeting date from agenda text using Claude."""
    prompt = f"""Extract the following from this NYC Council meeting agenda:

1. The committee name (e.g. "Committee on Housing and Buildings")
2. The meeting date

<agenda>
{agenda_text[:3000]}
</agenda>

Respond in exactly this format, nothing else:
COMMITTEE: [committee name]
DATE: YYYY-MM-DD"""

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    committee = ""
    date_str = ""
    for line in text.splitlines():
        if line.startswith("COMMITTEE:"):
            committee = line.split(":", 1)[1].strip()
        elif line.startswith("DATE:"):
            date_str = line.split(":", 1)[1].strip()

    logger.info(f"  Agenda metadata — committee: {committee}, date: {date_str}")
    return committee, date_str


def format_duration(duration_s):
    """Format a duration in seconds as a human-readable string."""
    if not duration_s:
        return ""
    total_minutes = int(duration_s) // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours == 0:
        return f"{minutes}m"
    elif hours == 1:
        return f"1hr {minutes}m"
    else:
        return f"{hours}hrs {minutes}m"


def ms_to_timestamp(ms):
    """Convert milliseconds to HH:MM:SS."""
    s = ms // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def youtube_url_at(url, ms):
    """Return `url` with a t={seconds}s deep-link, or None if url is falsy."""
    if not url:
        return None
    seconds = ms // 1000
    base, _, query = url.partition("?")
    parts = [p for p in query.split("&") if p and not p.startswith("t=")] if query else []
    if parts:
        return f"{base}?{'&'.join(parts)}&t={seconds}s"
    return f"{base}?t={seconds}s"


def timestamp_markdown(ms, youtube_url):
    """Build the bold timestamp line, linked to YouTube when a url is available."""
    ts = ms_to_timestamp(ms)
    link = youtube_url_at(youtube_url, ms)
    return f"[**({ts})**]({link})" if link else f"**({ts})**"


def enforce_timestamp_order(utterances):
    """Verify timestamps are monotonically non-decreasing and remove empty entries.

    Since segmentation now derives utterance timestamps directly from raw
    YouTube segments, regressions indicate a real bug — fail loudly rather
    than silently clamping.
    """
    result = []
    last_start = 0
    for u in utterances:
        if not u.get("text", "").strip():
            logger.warning(f"  Removing empty utterance at {ms_to_timestamp(u.get('start', 0))}")
            continue
        if u["start"] < last_start:
            raise RuntimeError(
                f"Timestamp regression in utterances: "
                f"{ms_to_timestamp(u['start'])} < {ms_to_timestamp(last_start)}. "
                "This should not happen with index-based segmentation."
            )
        last_start = u["start"]
        result.append(u)
    return result


def build_transcript_text(utterances):
    """Build plain text transcript from utterances for the API prompt."""
    lines = []
    for u in utterances:
        ts = ms_to_timestamp(u["start"])
        lines.append(f"[{ts}] {u['speaker']}: {u['text']}")
    return "\n".join(lines)


# Bump this when changing the segmentation contract so old caches are
# invalidated and re-segmented on the next run.
SEGMENTATION_VERSION = 2


def _format_seg_line(local_idx, seg):
    ts = ms_to_timestamp(seg["start_ms"])
    return f"{local_idx}\t[{ts}] {seg['text']}"


def segment_into_utterances(segments, agenda_text, client):
    """Use Claude to segment raw transcript into speaker-attributed utterances.

    Takes timed text segments (no speaker info) and asks Claude to return
    speaker-turn boundaries as segment-index ranges. Utterances are then
    built deterministically from raw_segments — Claude never produces
    timestamps or text directly, which eliminates timestamp hallucination
    and ordering bugs. Returns (utterances, speaker_map).
    """
    char_count = sum(len(_format_seg_line(i, s)) + 1 for i, s in enumerate(segments))

    if char_count > MAX_SEGMENT_CHARS:
        return _segment_chunked(segments, agenda_text, client)

    return _segment_single(segments, agenda_text, client)


def _segment_single(segments, agenda_text, client):
    """Segment a slice of raw segments that fits in one API call.

    Asks Claude to return turn boundaries as segment-index ranges, then
    deterministically reconstructs utterances from the raw segments.
    Returns (utterances, speaker_map).
    """
    n = len(segments)
    if n == 0:
        return [], {}

    numbered = "\n".join(_format_seg_line(i, seg) for i, seg in enumerate(segments))

    prompt = f"""You are analyzing a New York City Council meeting transcript to identify speakers and group consecutive segments into speaker turns.

This transcript comes from YouTube auto-generated captions. It has no speaker labels — you must identify who is speaking from context clues.

Here is the meeting agenda:

<agenda>
{agenda_text}
</agenda>

Here are the numbered transcript segments. Each line begins with an integer index, then a timestamp, then the segment text:

<segments>
{numbered}
</segments>

Your task:
1. Identify speaker changes based on context clues:
   - Self-introductions ("I am...", "My name is...", "Good morning, I'm...")
   - Addresses to others ("Thank you Council Member X", "Thank you Commissioner Y")
   - Chair announcements ("The chair recognizes...")
   - Roll calls
   - Changes in topic, tone or conversational turn-taking
   - The agenda listing who was invited to testify
2. Group consecutive segments belonging to the same speaker into a single turn.
3. Identify each speaker by their real name and role where you are at least 90% confident. Use "Speaker" for anyone you cannot confidently identify.

Return a JSON object with two keys:

"turns": an ordered list of speaker turns. Each turn is an object with:
  - "speaker": a short label like "A", "B", "C"
  - "first": index of the first segment in this turn (integer)
  - "last": index of the last segment in this turn (integer)

The turns must cover EVERY segment from index 0 to {n - 1} with no gaps and no overlaps. The first turn must start at 0 and the last turn must end at {n - 1}. Do NOT include timestamps or text in the turns — only the indices.

"speaker_map": a mapping from each label to the identified name/role, e.g.
  {{"A": "Chair Justin Brannan", "B": "Commissioner Jacques Jiha", "C": "Speaker"}}

Use "Speaker" (not "Speaker A" or "Unknown") for anyone you cannot identify with high confidence.

Return ONLY valid JSON, no other text."""

    logger.info(f"  Segmenting {n} segments into speaker turns...")
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        timeout=300.0,
    )

    response_text = response.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

    try:
        result = json.loads(response_text)
        turns = result["turns"]
        speaker_map = result.get("speaker_map", {})
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Could not parse speaker segmentation response: {e}")
        logger.error("Falling back to single-speaker turn for this chunk.")
        turns = [{"speaker": "A", "first": 0, "last": n - 1}]
        speaker_map = {"A": "Speaker"}

    turns = _validate_and_patch_turns(turns, n)

    # Ensure every turn label is present in speaker_map. The LLM occasionally
    # invents a label (e.g. literal "Speaker") that wasn't declared in its
    # speaker_map; without this, _segment_chunked's canonical_map lookup
    # KeyErrors and the whole segmentation pass is lost.
    for t in turns:
        if t["speaker"] not in speaker_map:
            logger.warning(f"  Turn label {t['speaker']!r} not in speaker_map; defaulting to 'Speaker'")
            speaker_map[t["speaker"]] = "Speaker"

    # Build utterances deterministically from raw segments
    utterances = []
    for t in turns:
        first = t["first"]
        last = t["last"]
        segs = segments[first:last + 1]
        if not segs:
            continue
        text_parts = [s["text"].strip() for s in segs if s.get("text", "").strip()]
        text = " ".join(text_parts)
        if not text:
            continue
        utterances.append({
            "speaker": t["speaker"],
            "start": segs[0]["start_ms"],
            "end": segs[-1].get("end_ms", segs[-1]["start_ms"]),
            "text": text,
        })

    logger.info(f"  Identified {len(utterances)} speaker turns.")
    if speaker_map:
        for label, name in sorted(speaker_map.items()):
            logger.info(f"    {label} -> {name}")

    return utterances, speaker_map


def _validate_and_patch_turns(turns, n):
    """Ensure turn ranges cover [0, n) without gaps or overlaps. Patch if needed."""
    cleaned = []
    for t in turns:
        try:
            first = int(t["first"])
            last = int(t["last"])
            speaker = str(t["speaker"])
        except (KeyError, ValueError, TypeError):
            logger.warning(f"  Dropping malformed turn: {t}")
            continue
        if first < 0 or last < first or last >= n:
            logger.warning(f"  Dropping out-of-range turn: {t} (n={n})")
            continue
        cleaned.append({"speaker": speaker, "first": first, "last": last})

    if not cleaned:
        logger.warning("  No valid turns from LLM; falling back to single turn.")
        return [{"speaker": "A", "first": 0, "last": n - 1}]

    cleaned.sort(key=lambda t: t["first"])

    patched = []
    cursor = 0
    for t in cleaned:
        if t["first"] > cursor:
            # Gap: extend the previous turn forward, or start a leading turn
            if patched:
                logger.warning(
                    f"  Patching gap [{cursor}, {t['first'] - 1}] -> previous speaker"
                )
                patched[-1]["last"] = t["first"] - 1
            else:
                logger.warning(
                    f"  Patching leading gap [0, {t['first'] - 1}] -> {t['speaker']}"
                )
                patched.append({"speaker": t["speaker"], "first": 0, "last": t["first"] - 1})
        elif t["first"] < cursor:
            # Overlap: trim the new turn's start
            logger.warning(f"  Trimming overlap: turn started at {t['first']}, cursor at {cursor}")
            t["first"] = cursor
            if t["first"] > t["last"]:
                continue
        patched.append(t)
        cursor = t["last"] + 1

    if cursor < n:
        logger.warning(f"  Patching trailing gap [{cursor}, {n - 1}] -> previous speaker")
        if patched:
            patched[-1]["last"] = n - 1
        else:
            patched.append({"speaker": "A", "first": 0, "last": n - 1})

    return patched


def _segment_chunked(segments, agenda_text, client):
    """Segment a long transcript by processing in chunks, then merge speakers."""
    # Build chunks of segments under MAX_SEGMENT_CHARS
    chunks = []  # list of segment slices
    current_chunk = []
    current_chars = 0
    for seg in segments:
        line = _format_seg_line(len(current_chunk), seg)
        if current_chars + len(line) > MAX_SEGMENT_CHARS and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0
        current_chunk.append(seg)
        current_chars += len(line) + 1
    if current_chunk:
        chunks.append(current_chunk)

    logger.info(f"Segmenting transcript in {len(chunks)} chunks...")

    all_utterances = []
    full_speaker_map = {}

    for k, chunk in enumerate(chunks, start=1):
        logger.info(f"  Processing chunk {k}/{len(chunks)} ({len(chunk)} segments)...")
        utterances, speaker_map = _segment_single(chunk, agenda_text, client)

        # Prefix labels per chunk so chunk N's "A" doesn't collide with chunk 1's "A"
        prefix = f"{k}-"
        speaker_map = {f"{prefix}{label}": name for label, name in speaker_map.items()}
        for u in utterances:
            u["speaker"] = f"{prefix}{u['speaker']}"

        all_utterances.extend(utterances)
        full_speaker_map.update(speaker_map)

    # Merge labels referring to the same identified person across chunks
    canonical_map = _consolidate_by_name(full_speaker_map)

    consolidated_speaker_map = {}
    for label, name in full_speaker_map.items():
        canon = canonical_map[label]
        existing = consolidated_speaker_map.get(canon)
        # Prefer a real identification over "Speaker" for the canonical label
        if not existing or existing == "Speaker":
            consolidated_speaker_map[canon] = name

    for u in all_utterances:
        u["speaker"] = canonical_map[u["speaker"]]

    # Merge consecutive utterances now belonging to the same speaker
    merged = []
    for u in all_utterances:
        if merged and merged[-1]["speaker"] == u["speaker"]:
            merged[-1]["text"] = merged[-1]["text"] + " " + u["text"]
            merged[-1]["end"] = u["end"]
        else:
            merged.append(dict(u))

    logger.info(
        f"  Consolidated {len(full_speaker_map)} per-chunk labels -> "
        f"{len(consolidated_speaker_map)} canonical speakers."
    )
    return merged, consolidated_speaker_map


def _consolidate_by_name(speaker_map):
    """Merge per-chunk labels that map to the same identified name.

    Labels mapping to "Speaker" stay separate — we cannot reliably tell
    unidentified speakers apart across chunks. Returns a mapping from each
    original label to a canonical label.
    """
    canonical = {}
    name_to_canon = {}
    counter = 0
    for label in sorted(speaker_map.keys()):
        name = speaker_map[label]
        if name == "Speaker":
            counter += 1
            canon = f"S{counter}"
        elif name in name_to_canon:
            canon = name_to_canon[name]
        else:
            counter += 1
            canon = f"S{counter}"
            name_to_canon[name] = canon
        canonical[label] = canon
    return canonical


def apply_speaker_names(utterances, speaker_map):
    """Replace speaker labels with identified names in utterances."""
    for u in utterances:
        if u["speaker"] in speaker_map:
            u["speaker"] = speaker_map[u["speaker"]]


def format_speakers(utterances, speaker_map):
    """Reformat speaker names for output: Chair/CM prefixes, witness first/subsequent."""
    # Classify each speaker from the speaker_map values
    CHAIR_PREFIX = "Chair "
    CM_PREFIXES = ("Councilmember ", "Council Member ", "CM ")
    # Roles that are neither council members nor witnesses (kept as-is)
    STAFF_ROLES = {"Committee Counsel", "Sergeant-at-Arms", "Committee Staff"}

    # Build classification: speaker_map value -> (role, display_first, display_subsequent)
    classifications = {}
    for label, name in speaker_map.items():
        if name.startswith(CHAIR_PREFIX):
            # Already in "Chair [Name]" format
            classifications[name] = ("chair", name, name)
        elif any(name.startswith(p) for p in CM_PREFIXES):
            # Strip prefix and re-add as "CM"
            for p in CM_PREFIXES:
                if name.startswith(p):
                    full_name = name[len(p):]
                    break
            classifications[name] = ("cm", f"CM {full_name}", f"CM {full_name}")
        elif name in STAFF_ROLES or name.startswith("Speaker "):
            classifications[name] = ("staff", name, name)
        else:
            # Witness: parse name, title, org from various formats
            # Format 1: "Name, Title for Org" or "Name, Title"
            # Format 2: "Name - Title"
            # Format 3: "Title Name" (e.g. "Commissioner Lisa Scott McKenzie")
            if ", " in name:
                # "Michael Sandler, Associate Commissioner for Office of ..."
                parts = name.split(", ", 1)
                full_name = parts[0]
                title_org = parts[1]
                first_display = f"{full_name}, {title_org}"
                subsequent_display = full_name
            elif " - " in name:
                # "Louisa Chaffee - IBO Director"
                parts = name.split(" - ", 1)
                full_name = parts[0]
                title_org = parts[1]
                first_display = f"{full_name}, {title_org}"
                subsequent_display = full_name
            else:
                # Could be "Commissioner Lisa Scott McKenzie" or just a name
                # Check for known title prefixes
                title_prefixes = [
                    "Commissioner ", "Deputy Commissioner ", "Associate Commissioner ",
                    "Assistant Commissioner ", "Director ", "Deputy Director ",
                    "Chief ", "Counsel ", "Public Advocate ",
                ]
                matched_prefix = None
                for tp in title_prefixes:
                    if name.startswith(tp):
                        matched_prefix = tp
                        break
                if matched_prefix:
                    full_name = name[len(matched_prefix):]
                    title = matched_prefix.strip()
                    first_display = f"{full_name}, {title}"
                    subsequent_display = full_name
                else:
                    # Just a name with no title info
                    full_name = name
                    first_display = name
                    subsequent_display = name

            classifications[name] = ("witness", first_display, subsequent_display)

    # Apply formatting with first/subsequent tracking
    seen_witnesses = set()
    for u in utterances:
        speaker = u["speaker"]
        if speaker in classifications:
            role, first_display, subsequent_display = classifications[speaker]
            if role == "witness":
                if speaker not in seen_witnesses:
                    u["speaker"] = first_display
                    seen_witnesses.add(speaker)
                else:
                    u["speaker"] = subsequent_display
            else:
                u["speaker"] = first_display
        else:
            # Unknown speaker not in speaker_map
            u["speaker"] = "Witness"


def remove_sections(utterances):
    """Remove oath (swearing in) and public testimony sections from transcript."""
    oath_pattern = re.compile(
        r"do you (swear|affirm).*(truth|honest)", re.IGNORECASE
    )
    # Match only true transition announcements (verb of opening/turning + public
    # testimony framing), not substantive speech that happens to mention "the public".
    public_testimony_pattern = re.compile(
        r"(open(s|ing)?\s+(the\s+|this\s+)?(hearing|meeting|floor)\s+(for|to|up\s+to)\s+(the\s+)?public"
        r"|(turn|move|proceed|go)(ing)?\s+(to|into)\s+(the\s+)?(remote\s+)?public\s+(testimony|comment)"
        r"|(we\s+)?will\s+now\s+hear\s+from\s+(members\s+of\s+)?the\s+public"
        r"|now\s+turn\s+to\s+(remote\s+)?(public\s+)?testimony"
        r"|first\s+(in[-\s]?person\s+)?panel\s+(of\s+the\s+public|to\s+testify))",
        re.IGNORECASE,
    )

    indices_to_remove = set()

    # Find and remove oath sections
    for i, u in enumerate(utterances):
        if oath_pattern.search(u["text"]):
            logger.info(f"  Removing oath at {ms_to_timestamp(u['start'])}")
            indices_to_remove.add(i)
            # Also remove adjacent short responses ("I do", "Yes", "Thank you. You may begin.")
            for j in range(i + 1, min(i + 4, len(utterances))):
                if len(utterances[j]["text"].strip()) < 80:
                    indices_to_remove.add(j)
                else:
                    break

    # Find and remove public testimony (everything from trigger phrase onward).
    # Only match when the phrase appears in a short utterance (transition
    # announcement) or near the start of the text, to avoid false positives
    # from passing mentions in longer speeches.
    public_start = None
    for i, u in enumerate(utterances):
        m = public_testimony_pattern.search(u["text"])
        if m and m.start() < 120:
            public_start = i
            logger.info(
                f"  Removing public testimony from {ms_to_timestamp(u['start'])} onward "
                f"({len(utterances) - i} utterances)"
            )
            break

    if public_start is not None:
        indices_to_remove.update(range(public_start, len(utterances)))

    if not indices_to_remove:
        logger.info("  No oath or public testimony sections found to remove.")
        return utterances

    result = [u for i, u in enumerate(utterances) if i not in indices_to_remove]
    logger.info(f"  Removed {len(indices_to_remove)} utterances total.")
    return result


def split_long_paragraphs(utterances, max_chars=1500):
    """Split oversized paragraphs within utterances at sentence boundaries."""
    for u in utterances:
        paragraphs = u["text"].split("\n\n")
        new_paragraphs = []
        for para in paragraphs:
            if len(para) <= max_chars:
                new_paragraphs.append(para)
                continue
            # Split at sentence boundaries
            sentences = re.split(r'(?<=[.!?])\s+', para)
            current = []
            current_len = 0
            for sent in sentences:
                if current and current_len + len(sent) > max_chars:
                    new_paragraphs.append(" ".join(current))
                    current = [sent]
                    current_len = len(sent)
                else:
                    current.append(sent)
                    current_len += len(sent)
            if current:
                new_paragraphs.append(" ".join(current))
        u["text"] = "\n\n".join(new_paragraphs)
    return utterances


MAX_CLEAN_CHARS = 18_000  # Max characters per transcript cleanup API call


def _preprocess_youtube_text(text):
    """Remove YouTube caption artifacts before LLM cleaning."""
    # Remove Unicode replacement characters
    text = text.replace("\ufffd", "")
    # Remove exact duplicate consecutive sentences
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if sentences:
        deduped = [sentences[0]]
        for s in sentences[1:]:
            if s.strip().lower() != deduped[-1].strip().lower():
                deduped.append(s)
        text = " ".join(deduped)
    return text


def extract_proper_nouns(agenda_text, client):
    """Use Claude to extract a reference list of proper nouns from the agenda."""
    if not agenda_text.strip():
        return ""

    prompt = f"""Extract all proper nouns from this New York City Council meeting agenda. Include:
- Full names of people (council members, commissioners, witnesses, staff)
- Committee names
- Organization and agency names (full names and acronyms)
- Legislation titles, bill numbers, resolution numbers
- Place names, building names, program names

Return ONLY a plain list, one item per line, no numbering or bullets. Include both the full name and any acronyms/abbreviations (e.g. "Department for the Aging" and "DFTA" on separate lines).

<agenda>
{agenda_text}
</agenda>"""

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _load_word_bank():
    """Load the persistent word bank from word_bank.json."""
    word_bank_path = os.path.join(os.path.dirname(__file__), "word_bank.json")
    if os.path.exists(word_bank_path):
        with open(word_bank_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"corrections": {}, "known_terms": []}


def _save_word_bank(word_bank):
    """Save the word bank back to word_bank.json."""
    word_bank_path = os.path.join(os.path.dirname(__file__), "word_bank.json")
    with open(word_bank_path, "w", encoding="utf-8") as f:
        json.dump(word_bank, f, indent=2, ensure_ascii=False)


def _accumulate_proper_nouns(proper_nouns_text):
    """Add newly extracted proper nouns to the word bank's known_terms list."""
    if not proper_nouns_text.strip():
        return
    word_bank = _load_word_bank()
    existing = set(t.lower() for t in word_bank["known_terms"])
    new_terms = [
        line.strip()
        for line in proper_nouns_text.splitlines()
        if line.strip() and line.strip().lower() not in existing
    ]
    if new_terms:
        word_bank["known_terms"].extend(new_terms)
        _save_word_bank(word_bank)
        logger.info(f"  Added {len(new_terms)} new terms to word bank.")


def clean_transcript(utterances, proper_nouns, client):
    """Clean transcript utterances: fix punctuation, remove fillers, correct proper nouns.

    Processes utterances in batches to stay within API token limits.
    Returns a new list of utterances with cleaned text.
    """
    logger.info("Cleaning transcript text...")

    # Load word bank for corrections and known terms
    word_bank = _load_word_bank()
    corrections_text = "\n".join(
        f"  {wrong} → {right}"
        for wrong, right in word_bank.get("corrections", {}).items()
    )
    known_terms_text = "\n".join(
        f"  {term}" for term in word_bank.get("known_terms", [])
    )

    # Build batches of utterances, keeping each batch under MAX_CLEAN_CHARS
    batches = []
    current_batch = []
    current_chars = 0

    for i, u in enumerate(utterances):
        text_len = len(u["text"])
        if current_chars + text_len > MAX_CLEAN_CHARS and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append((i, u))
        current_chars += text_len

    if current_batch:
        batches.append(current_batch)

    logger.info(f"  Processing {len(batches)} batch(es)...")

    cleaned = list(utterances)  # shallow copy

    for batch_num, batch in enumerate(batches):
        logger.info(f"  Cleaning batch {batch_num + 1}/{len(batches)}...")

        # Build input with clear separators, pre-processing YouTube artifacts
        separator = "---UTTERANCE---"
        batch_lines = []
        for idx, (orig_i, u) in enumerate(batch):
            preprocessed = _preprocess_youtube_text(u["text"])
            batch_lines.append(f"[{idx}]\n{preprocessed}\n{separator}")
        batch_text = "\n".join(batch_lines)

        prompt = f"""You are cleaning a speech-to-text transcript of a New York City Council meeting. This transcript was auto-generated from YouTube captions, which frequently mishear words, drop words, and produce garbled text.

Your guiding principle: produce readable, flowing text. YouTube auto-captions are noisy — they repeat phrases, garble names, and drop words — but a human listener could usually understand what was said. Your job is to reconstruct the most likely intended speech from the garbled captions. Accept minor inaccuracies over gaps. Use "..." only when a section is so garbled that no reasonable interpretation exists.

Rules:

Handling unclear or garbled text:
- YouTube captions frequently repeat phrases, drop words, and garble names. Reconstruct what the speaker most likely said based on surrounding context
- If a word is misheard but you can infer the intended word from context, replace it with your best guess
- Use "..." ONLY when multiple consecutive words are completely unintelligible and no reasonable reconstruction is possible. A single unclear word should be your best guess, not an ellipsis
- If a phrase or sentence is repeated (a common YouTube caption artifact), keep only one clean instance
- Do NOT insert editorial tags, brackets, annotations, or comments such as [VERIFY], [unclear], [inaudible], or [?]

Proper nouns and known terms:
- Correct proper nouns ONLY when the transcript version is a clear phonetic misspelling of a name on one of the reference lists below, AND the misspelled version is not itself a real word or name
- Apply the known corrections from the word bank below — these are definitive and should always be applied
- Use the known terms list to recognise NYC government terminology that should be preserved as-is

Style rules:
- Fix punctuation and sentence boundaries (the transcript often has run-on sentences)
- Remove verbal fillers ("uh", "um", "you know", false starts, repeated words)
- Expand all contractions to their full form (e.g. "what's" to "what is", "they're" to "they are", "it's" to "it is", "we're" to "we are")
- Do NOT use Oxford commas (in lists of three or more, no comma before "and" or "or")
- Do NOT use comma splices. If two independent clauses are joined by only a comma, use a period and start a new sentence
- Where a comma followed by a conjunction (and, but, or, so, yet) joins two independent clauses that could each stand alone, prefer breaking into two separate sentences if the original meaning can be maintained
- Use ellipsis ("...") rather than em dashes ("—") to signify a pause or trailing off in speech
- Capitalise "Council" when referring to the NYC Council specifically, "City" when referring to New York City specifically, and "Bill" when referring to a specific bill
- Replace "Councilmember" or "Council Member" with "CM" in the text
- Break long paragraphs: if an utterance exceeds roughly 10 lines, insert a blank line at natural topic shifts to create shorter paragraphs within the utterance

Structural rules:
- Do NOT change the meaning, rephrase, paraphrase, or summarize
- Do NOT merge or split utterances — return exactly the same number of entries
- Keep the same [index] numbers

<proper_nouns>
{proper_nouns}
</proper_nouns>

<word_bank_corrections>
{corrections_text}
</word_bank_corrections>

<known_terms>
{known_terms_text}
</known_terms>

Transcript to clean (each utterance starts with [index] and ends with {separator}):

<transcript>
{batch_text}
</transcript>

Return the cleaned transcript in EXACTLY the same format:
[index]
cleaned text here
{separator}

You MUST return the same number of entries with the same index numbers. Return ONLY the cleaned transcript, no other text."""

        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=16000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            timeout=300.0,
        )
        cleaned_text = response.content[0].text.strip()

        if response.stop_reason == "max_tokens":
            logger.warning(f"  WARNING: Batch {batch_num + 1} was truncated by max_tokens limit!")

        # Parse using separator-based splitting
        entries = cleaned_text.split(separator)
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            # Match [index] followed by the cleaned text
            m = re.match(r"\[(\d+)\]\s*(.*)", entry, re.DOTALL)
            if m:
                idx = int(m.group(1))
                text = m.group(2).strip()
                # Post-processing: strip any bracket annotations the LLM may have added
                text = re.sub(r"\[(?:VERIFY|unclear|inaudible|sic|\?)[^\]]*\]", "", text)
                text = re.sub(r"  +", " ", text).strip()
                if 0 <= idx < len(batch):
                    orig_i = batch[idx][0]
                    cleaned[orig_i] = {**utterances[orig_i], "text": text}

    logger.info("  Transcript cleaning complete.")
    return cleaned


def clean_summary(text):
    """Post-process summary to strip formatting and enforce structure."""
    # Apply word bank corrections.
    word_bank_path = Path(__file__).parent / "word_bank.json"
    if word_bank_path.exists():
        with open(word_bank_path, "r", encoding="utf-8") as f:
            word_bank = json.load(f)
        for wrong, right in word_bank.get("corrections", {}).items():
            text = text.replace(wrong, right)

    # Strip inline/markdown formatting characters.
    text = re.sub(r'\*\*|##|#|\*|_|`', '', text)

    # Normalize list prefixes (bullet variants and numbered lists) to "- ".
    text = re.sub(r'^(?:[•*]\s+|\d+[.)]\s+)', '- ', text, flags=re.MULTILINE)

    # Ensure section titles appear on their own lines with a blank line before.
    for title in ('Summary', 'Meeting Overview', 'Numbers', 'Action Points'):
        text = re.sub(
            rf'(?<!\n)\n(?={re.escape(title)}\n)', '\n\n', text
        )

    # Ensure list items end with a full stop.
    text = re.sub(r'^(- \S.+[^.\n])$', r'\1.', text, flags=re.MULTILINE)

    # Trim trailing whitespace on each line and collapse 3+ blank lines to 2.
    lines = [line.rstrip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def generate_summary(utterances, agenda_text):
    """Use Anthropic API to generate a structured summary."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error(" ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    transcript_text = build_transcript_text(utterances)

    # If transcript is very long, chunk and summarize in stages.
    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        logger.info("Long transcript detected. Summarizing in chunks...")
        summary = _summarize_long(client, transcript_text, agenda_text)
    else:
        summary = _summarize_single(client, transcript_text, agenda_text)

    summary = clean_summary(summary)
    return summary


def _summarize_single(client, transcript_text, agenda_text):
    """Summarize a transcript that fits in one API call."""
    logger.info("Generating summary...")
    prompt = f"""You are summarizing a New York City Council meeting. Be concise and factual. Focus on substance, not formalities. Write in the style of an expert on US urban policy, especially NYC, who has the contextual knowledge to understand whether and how a meeting matters beyond its immediate context; is very passionate about the importance of urban policy; is pro-good government and modernising public services using technology; is sceptical about government competence; is interested in arguments and action, not procedural formalities; and has a dry, wry sense of humour where appropriate.

Here is the meeting agenda:

<agenda>
{agenda_text}
</agenda>

Here is the full transcript with speaker labels and timestamps:

<transcript>
{transcript_text}
</transcript>

Write a summary structured as follows. Use plain text only — no markdown, no inline formatting (no **, ##, *, _, ` etc.):

Summary
[This line stands alone as a title at the top.]

Meeting Overview
[3-5 paragraphs scaled to the substance of the hearing. Cover what was discussed, what arguments were made, what (if anything) was decided, why decisions were made, and who provided testimony and what they argued. Reference bill numbers and topic names naturally within the narrative. Do not pad — shorter or less-substantial meetings should have shorter summaries.]

Numbers
[A list of all relevant numbers, statistics and budget figures from the meeting, each on its own line prefixed with "- ". Do not include procedural references like "page 4 of the report". Deduplicate numbers which appear more than once. Each number should appear once with enough context to be understood on its own. End each list item with a full stop.]

Action Points
[A list of all action points from the meeting, each on its own line prefixed with "- ". Where relevant, specify the owner: "- HPD to follow up with committee on tree cost." Only include items where a specific actor committed to a specific action or was asked to do a specific action. Do not include general uncertainties or open questions: those should be included in the meeting overview section. End each list item with a full stop.]

No other sections."""

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _summarize_long(client, transcript_text, agenda_text):
    """Summarize a long transcript by chunking."""
    # Split transcript into chunks.
    chunks = []
    while transcript_text:
        chunks.append(transcript_text[:MAX_TRANSCRIPT_CHARS])
        transcript_text = transcript_text[MAX_TRANSCRIPT_CHARS:]

    logger.info(f"Processing {len(chunks)} chunks...")
    chunk_summaries = []

    for i, chunk in enumerate(chunks):
        logger.info(f"  Summarizing chunk {i + 1}/{len(chunks)}...")
        prompt = f"""You are summarizing part {i + 1} of {len(chunks)} of a New York City Council meeting transcript.

Here is the meeting agenda for context:

<agenda>
{agenda_text}
</agenda>

Here is this portion of the transcript:

<transcript>
{chunk}
</transcript>

Summarize what was discussed and what arguments were made.

Numbers:
List all numbers, statistics and budget figures, each on its own line prefixed with "- ".

Action points:
List all action points, each on its own line prefixed with "- ". Only include items where a specific actor committed to a specific action or was asked to do a specific action. Do not include general uncertainties or open questions.

Note any unresolved threads."""

        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        chunk_summaries.append(response.content[0].text)

    # Combine chunk summaries into final summary.
    logger.info("  Combining into final summary...")
    combined = "\n\n---\n\n".join(
        f"Part {i + 1}:\n{s}" for i, s in enumerate(chunk_summaries)
    )

    prompt = f"""You are producing the final summary of a New York City Council meeting. Be concise and factual. Focus on substance, not formalities. Write in the style of an expert on US urban policy, especially NYC, who has the contextual knowledge to understand whether and how a meeting matters beyond its immediate context; is very passionate about the importance of urban policy; is pro-good government and modernising public services using technology; is sceptical about government competence; is interested in arguments and action, not procedural formalities; and has a dry, wry sense of humour where appropriate.

Here is the meeting agenda:

<agenda>
{agenda_text}
</agenda>

Here are summaries of each portion of the meeting:

{combined}

Write a summary of the FULL meeting structured as follows. Use plain text only — no markdown, no inline formatting (no **, ##, *, _, ` etc.):

Summary
[This line stands alone as a title at the top.]

Meeting Overview
[3-5 paragraphs scaled to the substance of the hearing. Cover what was discussed, what arguments were made, what (if anything) was decided, why decisions were made, and who provided testimony and what they argued. Reference bill numbers and topic names naturally within the narrative. Do not pad — shorter or less-substantial meetings should have shorter summaries.]

Numbers
[A list of all relevant numbers, statistics and budget figures from the meeting, each on its own line prefixed with "- ". Do not include procedural references like "page 4 of the report". Deduplicate numbers which appear more than once. Each number should appear once with enough context to be understood on its own. End each list item with a full stop.]

Action Points
[A list of all action points from the meeting, each on its own line prefixed with "- ". Where relevant, specify the owner: "- HPD to follow up with committee on tree cost." Only include items where a specific actor committed to a specific action or was asked to do a specific action. Do not include general uncertainties or open questions: those should be included in the meeting overview section. End each list item with a full stop.]

No other sections."""

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def align_agenda_items(utterances, agenda_text):
    """Try to extract agenda item headings and roughly align them to timestamps.

    Returns a list of (start_ms, heading) tuples, sorted by start_ms.
    This is best-effort: if no items are found, returns an empty list.
    """
    # Extract numbered agenda items from agenda text.
    patterns = [
        r"(?m)^\s*(?:Item\s+)?(\d+)[.\)]\s+(.+)",
        r"(?m)^([IVXLC]+)[.\)]\s+(.+)",
    ]
    items = []
    for pat in patterns:
        for m in re.finditer(pat, agenda_text):
            items.append(m.group(0).strip())
    if not items:
        return []

    # Try to find mentions of agenda items in the transcript.
    aligned = []
    for item in items:
        # Search for keywords from the item in utterances.
        keywords = [w.lower() for w in item.split() if len(w) > 3]
        if not keywords:
            continue
        for u in utterances:
            text_lower = u["text"].lower()
            matches = sum(1 for kw in keywords if kw in text_lower)
            if matches >= min(2, len(keywords)):
                aligned.append((u["start"], item))
                break

    aligned.sort(key=lambda x: x[0])
    return aligned


def build_web_content(summary, utterances, agenda_text, title, slug, date_str,
                      youtube_url="", duration="", council_url=""):
    """Build a markdown file with YAML front matter for the website."""
    # Summary section
    summary_lines = [summary]

    # Transcript section
    transcript_lines = []
    agenda_markers = align_agenda_items(utterances, agenda_text)
    marker_idx = 0
    for u in utterances:
        while marker_idx < len(agenda_markers):
            marker_ts, marker_heading = agenda_markers[marker_idx]
            if u["start"] >= marker_ts:
                transcript_lines.append(f"### {marker_heading}")
                marker_idx += 1
            else:
                break
        transcript_lines.append(timestamp_markdown(u["start"], youtube_url))
        transcript_lines.append("")
        transcript_lines.append(u["text"])

    lines = [
        "---",
        f'title: "{title}"',
        f"date: {date_str}",
        f"slug: {slug}",
        f'duration: "{duration}"',
        f'youtube_url: "{youtube_url}"',
    ]
    if council_url:
        lines.append(f'council_url: "{council_url}"')
    lines += [
        "---",
        "",
        "\n\n".join(summary_lines),
        "",
        "## Full Transcript",
        "",
        "\n\n".join(transcript_lines),
    ]
    return "\n".join(lines)


def publish_to_website(web_content, slug, title, deploy=True):
    """Save markdown to the website content dir, build the site, and push."""
    if not WEBSITE_CONTENT_DIR.exists():
        logger.error(f"Website content directory not found: {WEBSITE_CONTENT_DIR}")
        sys.exit(1)

    web_path = WEBSITE_CONTENT_DIR / f"{slug}.md"
    with open(web_path, "w", encoding="utf-8") as f:
        f.write(web_content)
    logger.info(f"Markdown saved to: {web_path}")

    if not deploy:
        logger.info("Skipping site build and git push (--no-deploy).")
        return

    # Build the site
    site_dir = WEBSITE_CONTENT_DIR.parent / "site"
    repo_dir = WEBSITE_CONTENT_DIR.parent
    logger.info("Building website...")
    result = subprocess.run(
        [sys.executable, "build.py"],
        cwd=str(site_dir),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(f"Build failed:\n{result.stderr}")
        sys.exit(1)
    logger.info("Site built successfully.")

    # Git add, commit, push
    logger.info("Committing and pushing to deploy...")
    commit_msg = f"Add summary: {title}"
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True)
    result = subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(repo_dir),
                            capture_output=True, text=True)
    if result.returncode != 0:
        if "nothing to commit" in result.stdout:
            logger.info("No changes to commit.")
            return
        logger.error(f"Git commit failed:\n{result.stderr}")
        sys.exit(1)
    subprocess.run(["git", "push"], cwd=str(repo_dir), check=True)
    logger.info(f"Pushed to remote. Cloudflare will deploy shortly.")


def main():
    args = parse_args()

    # When --transcript-json is used, argparse assigns the single positional to youtube_url.
    # Shift it to agenda_pdf if needed.
    if not args.agenda_pdf and args.youtube_url:
        args.agenda_pdf = args.youtube_url
        args.youtube_url = None

    # Validate agenda PDF exists.
    if not args.agenda_pdf:
        logger.error("agenda_pdf is required.")
        sys.exit(1)
    agenda_path = Path(args.agenda_pdf)
    if not agenda_path.exists():
        logger.error(f"Agenda PDF not found: {agenda_path}")
        sys.exit(1)

    # Ensure directories exist.
    INPUT_DIR.mkdir(exist_ok=True)

    # Step 1: Fetch YouTube transcript.
    if args.transcript_json:
        json_path = Path(args.transcript_json)
        if not json_path.exists():
            logger.error(f"Transcript JSON not found: {json_path}")
            sys.exit(1)
        logger.info(f"Using specified transcript: {json_path.name}")
        with open(json_path, "r", encoding="utf-8") as f:
            cached_data = json.load(f)
        segments = cached_data.get("raw_segments", [])
        video_info = {"title": cached_data.get("title", json_path.stem)}
        # Reconstruct youtube_url and fetch duration from cached video_id.
        video_id = cached_data.get("video_id")
        if video_id:
            if not args.youtube_url:
                args.youtube_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                    info = ydl.extract_info(args.youtube_url, download=False)
                    video_info["duration"] = info.get("duration", 0)
            except Exception as e:
                logger.warning(f"Could not fetch video duration: {e}")
    else:
        if not args.youtube_url:
            logger.error("Either youtube_url or --transcript-json is required.")
            sys.exit(1)
        # Cost estimate and confirmation.
        video_info = estimate_costs_and_confirm(args.youtube_url)
        # Fetch transcript.
        segments, json_path = fetch_youtube_transcript(
            args.youtube_url, video_info, skip=args.skip_fetch
        )

    # Step 2: Parse agenda and extract metadata.
    agenda_text = parse_agenda(agenda_path)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    committee_name, meeting_date = extract_agenda_metadata(agenda_text, client)

    video_title = (video_info.get("title") or "").lower()
    committee_words = [w for w in committee_name.lower().split() if len(w) > 3]
    committee_match = any(w in video_title for w in committee_words)
    date_match = meeting_date in video_title or (
        meeting_date and meeting_date.replace("-", "") in video_title.replace("/", "").replace("-", "")
    )
    if not committee_match and not date_match:
        logger.warning(f"Possible mismatch — agenda says '{committee_name}' on {meeting_date}, "
                       f"but video title is '{video_info.get('title', '')}'")
        response = input("Agenda and video may not match. Continue? [y/N] ").strip()
        if response.lower() != "y":
            logger.info("Aborted.")
            sys.exit(0)

    # Step 3: Segment into speaker turns.
    # Check if utterances are cached in the JSON transcript file.
    cached_data = None
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            cached_data = json.load(f)

    cached_seg_version = (
        cached_data.get("segmentation_version") if isinstance(cached_data, dict) else None
    )
    if (
        isinstance(cached_data, dict)
        and "utterances" in cached_data
        and cached_seg_version == SEGMENTATION_VERSION
    ):
        utterances = cached_data["utterances"]
        speaker_map = cached_data.get("speaker_map", {})
        logger.info("Using cached speaker-segmented transcript.")
        if cached_data.get("agenda_pdf") != agenda_path.name:
            cached_data["agenda_pdf"] = agenda_path.name
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(cached_data, f, indent=2, ensure_ascii=False)
    else:
        if isinstance(cached_data, dict) and "utterances" in cached_data:
            logger.info(
                f"Cached segmentation is version {cached_seg_version}, "
                f"current is {SEGMENTATION_VERSION}; re-segmenting."
            )
            # Drop stale dependent caches too
            cached_data.pop("cleaned_utterances", None)
        utterances, speaker_map = segment_into_utterances(segments, agenda_text, client)

        # Cache utterances and speaker map
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                cache_obj = json.load(f)
        else:
            cache_obj = {}
        cache_obj["utterances"] = utterances
        cache_obj["speaker_map"] = speaker_map
        cache_obj["segmentation_version"] = SEGMENTATION_VERSION
        cache_obj["agenda_pdf"] = agenda_path.name
        # Stale cleaned utterances are tied to old utterance boundaries
        cache_obj.pop("cleaned_utterances", None)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cache_obj, f, indent=2, ensure_ascii=False)
        # Refresh in-memory snapshot so the cleaned_utterances check below
        # sees the current cache state.
        cached_data = cache_obj

    utterances = enforce_timestamp_order(utterances)

    if speaker_map:
        apply_speaker_names(utterances, speaker_map)

    # Step 4: Clean transcript.
    # Always load cached cleaned utterances if available, even with --skip-clean.
    if isinstance(cached_data, dict) and "cleaned_utterances" in cached_data:
        logger.info("Using cached cleaned transcript.")
        utterances = cached_data["cleaned_utterances"]
        if speaker_map:
            apply_speaker_names(utterances, speaker_map)
    elif not args.skip_clean:
        proper_nouns = extract_proper_nouns(agenda_text, client)
        logger.info(f"Extracted {len(proper_nouns.splitlines())} proper nouns from agenda.")
        _accumulate_proper_nouns(proper_nouns)

        utterances = clean_transcript(utterances, proper_nouns, client)

        # Cache cleaned utterances in the JSON file
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                cache_obj = json.load(f)
        else:
            cache_obj = {}
        # Store cleaned utterances with original speaker labels for caching
        cleaned_for_cache = []
        for u in utterances:
            cached_u = dict(u)
            if speaker_map:
                for label, name in speaker_map.items():
                    if cached_u["speaker"] == name:
                        cached_u["speaker"] = label
                        break
            cleaned_for_cache.append(cached_u)
        cache_obj["cleaned_utterances"] = cleaned_for_cache
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cache_obj, f, indent=2, ensure_ascii=False)
        logger.info("Cleaned transcript cached.")

    # Step 5: Format speaker headers.
    if speaker_map:
        format_speakers(utterances, speaker_map)

    # Step 6: Remove oath and public testimony.
    utterances = remove_sections(utterances)

    # Step 7: Strip residual [VERIFY:] markers.
    verify_pattern = re.compile(r"\s*\[VERIFY:[^\]]*\]")
    for u in utterances:
        u["text"] = verify_pattern.sub("", u["text"])

    # Step 8: Split oversized paragraphs in transcript.
    split_long_paragraphs(utterances)

    # Step 9: Build markdown and publish to website.
    if args.skip_summary:
        # Reuse existing published page, only replace the transcript section.
        slug = args.skip_summary
        existing_path = WEBSITE_CONTENT_DIR / f"{slug}.md"
        if not existing_path.exists():
            logger.error(f"Existing page not found: {existing_path}")
            sys.exit(1)
        with open(existing_path, "r", encoding="utf-8") as f:
            existing_content = f.read()
        # Split at "## Full Transcript" and keep everything above it.
        marker = "## Full Transcript"
        if marker not in existing_content:
            logger.error(f"Could not find '{marker}' in existing page")
            sys.exit(1)
        header = existing_content.split(marker)[0]
        # Pull youtube_url from the existing page's YAML front matter so
        # regenerated timestamps can link back to the video.
        yt_match = re.search(r'^youtube_url:\s*"([^"]*)"', header, re.MULTILINE)
        youtube_url = yt_match.group(1) if yt_match else (args.youtube_url or "")
        # Build new transcript section.
        transcript_lines = []
        agenda_markers = align_agenda_items(utterances, agenda_text)
        marker_idx = 0
        for u in utterances:
            while marker_idx < len(agenda_markers):
                marker_ts, marker_heading = agenda_markers[marker_idx]
                if u["start"] >= marker_ts:
                    transcript_lines.append(f"### {marker_heading}")
                    marker_idx += 1
                else:
                    break
            transcript_lines.append(timestamp_markdown(u["start"], youtube_url))
            transcript_lines.append("")
            transcript_lines.append(u["text"])
        web_content = header + marker + "\n\n" + "\n\n".join(transcript_lines)
        title = slug  # For logging only
        logger.info("Skipping summary generation, reusing existing page.")
    else:
        # Generate summary.
        summary = generate_summary(utterances, agenda_text)

        if args.title and committee_name:
            title = f"{committee_name}, {args.title}"
        elif args.title:
            title = args.title
        else:
            title = video_info.get("title", "council_meeting")
            title = clean_youtube_title(title)

        slug = re.sub(r"[^\w\s-]", "", title.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        date_str = meeting_date or datetime.now().strftime("%Y-%m-%d")
        youtube_url = args.youtube_url or ""
        duration = format_duration(video_info.get("duration", 0))

        web_content = build_web_content(
            summary, utterances, agenda_text, title, slug, date_str, youtube_url,
            duration, council_url=args.council_url or ""
        )
    publish_to_website(web_content, slug, title, deploy=not args.no_deploy)

    logger.info("Done!")


if __name__ == "__main__":
    main()
