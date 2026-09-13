#!/usr/bin/env python3
"""Build the record layer: what the Council actually decided, per meeting.

Phase 1 of the expansion. Read-only with respect to the site -- this writes
``data/`` and nothing else. No Anthropic calls, no cost beyond HTTP.

    python scrape_record.py                 # every meeting in the archive
    python scrape_record.py --since 2026-09-01
    python scrape_record.py --limit 5 --dry-run

Meetings come from two places, neither of which needs the Legistar
date-range postback:

1. ``content/*.md`` front matter -- every published hearing records its own
   ``council_url`` with event id and GUID. 72 of 77 carry one.
2. ``Calendar.aspx`` -- the live ~100-row window, which picks up meetings
   that were never published (vote sessions, Stated Meetings).

Output, one small JSON file per entity so a nightly run produces a readable
git diff rather than an opaque blob:

    data/meetings/<event_id>.json    items, actions, roll calls
    data/matters/<file-slug>.json    status, sponsors, attachments, history
"""

import argparse
import glob
import html
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pdfplumber
import requests

from council_scraper import BROWSER_HEADERS, LEGISTAR_HOST, list_calendar_events

REPO_ROOT = Path(__file__).parent
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = REPO_ROOT / "Input" / "legistar"
DOC_CACHE_DIR = REPO_ROOT / "Input" / "legistar_docs"
CONTENT_DIR = REPO_ROOT / "content"

# Legistar is a public service with no API. Space requests out rather than
# issuing several hundred back to back.
REQUEST_DELAY = 0.2

logger = logging.getLogger("record")


# --- fetching -----------------------------------------------------------


class Fetcher:
    """GET with an on-disk cache, so re-runs cost nothing and dev is cheap."""

    def __init__(self, use_cache=True):
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.use_cache = use_cache
        self.fetched = 0
        self.cached = 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, path):
        url = path if path.startswith("http") else f"{LEGISTAR_HOST}/{path}"
        key = re.sub(r"[^A-Za-z0-9]+", "_", url)[-120:]
        cache_file = CACHE_DIR / f"{key}.html"
        if self.use_cache and cache_file.exists():
            self.cached += 1
            return cache_file.read_text(encoding="utf-8")
        time.sleep(REQUEST_DELAY)
        r = self.session.get(url, timeout=45)
        r.raise_for_status()
        self.fetched += 1
        cache_file.write_text(r.text, encoding="utf-8")
        return r.text

    def get_bytes(self, url):
        """Fetch a binary attachment, cached the same way.

        Documents are cached under Input/ (gitignored) and never committed:
        they are Legistar's to host, and 2,800 of them would bloat the repo.
        """
        key = re.sub(r"[^A-Za-z0-9]+", "_", url)[-120:]
        cache_file = DOC_CACHE_DIR / f"{key}.bin"
        if self.use_cache and cache_file.exists():
            self.cached += 1
            return cache_file.read_bytes()
        time.sleep(REQUEST_DELAY)
        r = self.session.get(url, timeout=90)
        r.raise_for_status()
        self.fetched += 1
        DOC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(r.content)
        return r.content


# --- HTML tables --------------------------------------------------------


class _Rows(HTMLParser):
    """Collect table rows as lists of {text, links} cells.

    Legistar hides the per-row "Action details" target in an onclick
    (``radopen('HistoryDetail.aspx?ID=...')``) rather than an href, so
    onclick is scanned for URLs alongside href.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None
        self._links = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._links = [], []
        elif tag == "a" and self._cell is not None:
            for raw in (a.get("href", ""), a.get("onclick", "")):
                for m in re.finditer(r"(?:Legislation|History)Detail\.aspx\?[^'\"&\s]*(?:&[^'\"\s]*)*", raw):
                    self._links.append(html.unescape(m.group(0)).rstrip("',"))

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append({
                "text": re.sub(r"\s+", " ", "".join(self._cell)).strip(),
                "links": self._links,
            })
            self._cell = self._links = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def table_rows(page_html):
    p = _Rows()
    p.feed(page_html)
    return p.rows


def labelled_field(page_html, label):
    """Pull a `Label: <span>value</span>` field off a Legistar detail page.

    Try the span wrapper before the anchor one. Several values nest a link
    inside the span ("Linda Lee" inside Sponsors, which also carries
    "(by request of the Mayor)"), and matching the anchor first closes the
    capture early and silently truncates the value.
    """
    for tag in ("span", "a"):
        m = re.search(
            rf">{re.escape(label)}:?\s*<[^>]*>.{{0,400}}?<{tag}[^>]*>(.*?)</{tag}>",
            page_html, re.S,
        )
        if m:
            return re.sub(r"\s+", " ",
                          re.sub(r"<[^>]+>", " ", html.unescape(m.group(1)))).strip()
    return ""


# --- meeting discovery --------------------------------------------------

EVENT_RE = re.compile(r"MeetingDetail\.aspx\?ID=(\d+)&GUID=([0-9A-Fa-f-]+)")


def archive_meetings():
    """Event id -> (GUID, date) for every published hearing that records one.

    The date comes from the hearing's own front matter, so the caller can
    apply --since without fetching anything.
    """
    found = {}
    for path in sorted(glob.glob(str(CONTENT_DIR / "*.md"))):
        head = Path(path).read_text(encoding="utf-8", errors="replace")[:3000]
        m = EVENT_RE.search(head)
        if m:
            d = re.search(r"^date:\s*(\S+)", head, re.M)
            found[m.group(1)] = (m.group(2), d.group(1) if d else "")
    return found


def calendar_meetings():
    """Event id -> (GUID, date) for everything on the live calendar window."""
    return {e["event_id"]: (e["event_guid"], e.get("event_date", ""))
            for e in list_calendar_events() if e.get("event_guid")}


# --- scraping -----------------------------------------------------------


def matter_slug(file_number):
    return re.sub(r"[^a-z0-9]+", "-", file_number.lower()).strip("-")


def scrape_roll_call(fetcher, history_path):
    """Return [{name, vote}] for one action, or [] when no vote was recorded."""
    page = fetcher.get(history_path)
    votes = []
    seen_header = False
    for row in table_rows(page):
        if len(row) < 2:
            continue
        first = row[0]["text"]
        if first.lower().startswith("person name"):
            seen_header = True
            continue
        if seen_header and first and "No records" not in first:
            votes.append({"name": first, "vote": row[1]["text"]})
    return votes


def scrape_meeting(fetcher, event_id, guid, with_votes=True):
    """Scrape one MeetingDetail page into a record."""
    url = f"{LEGISTAR_HOST}/MeetingDetail.aspx?ID={event_id}&GUID={guid}&Search="
    page = fetcher.get(url)

    rows = table_rows(page)
    header = next((r for r in rows if r and r[0]["text"] == "File #"), None)
    if not header:
        logger.warning("  %s: no items grid", event_id)
        return None
    cols = {c["text"].replace("\xa0", " "): i for i, c in enumerate(header)}

    def cell(row, name):
        i = cols.get(name)
        return row[i]["text"] if i is not None and i < len(row) else ""

    items = []
    for row in rows:
        if row is header or len(row) < len(cols) - 2:
            continue
        file_number = cell(row, "File #")
        if not file_number or file_number == "File #":
            continue
        legislation = next((l for c in row for l in c["links"]
                            if l.startswith("LegislationDetail")), "")
        history = next((l for c in row for l in c["links"]
                        if l.startswith("HistoryDetail")), "")
        item = {
            "file_number": file_number,
            "type": cell(row, "Type"),
            "name": cell(row, "Name"),
            "sponsor": cell(row, "Prime Sponsor"),
            "agenda_note": cell(row, "Agenda Note"),
            "action": cell(row, "Action"),
            "result": cell(row, "Result"),
            "legistar_id": (re.search(r"ID=(\d+)", legislation) or [None, ""])[1],
            # LegislationDetail 410s without its GUID, so carry it through.
            "legistar_guid": (re.search(r"GUID=([0-9A-Fa-f-]+)", legislation) or [None, ""])[1],
        }
        if with_votes and history:
            roll = scrape_roll_call(fetcher, history)
            if roll:
                tally = {}
                for v in roll:
                    tally[v["vote"]] = tally.get(v["vote"], 0) + 1
                item["tally"] = tally
                item["roll_call"] = roll
        items.append(item)

    return {
        "event_id": event_id,
        "date": _iso_date(labelled_field(page, "Meeting date/time")),
        "body": labelled_field(page, "Meeting Name"),
        "location": labelled_field(page, "Meeting location"),
        "agenda_status": labelled_field(page, "Agenda status"),
        "minutes_status": labelled_field(page, "Minutes status"),
        "council_url": url,
        "items": items,
    }


def _iso_date(raw):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw or "")
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def scrape_matter(fetcher, legistar_id, guid, file_number):
    """Scrape one LegislationDetail page into a matter record."""
    page = fetcher.get(
        f"LegislationDetail.aspx?ID={legistar_id}&GUID={guid}&Options=&Search="
    )
    if not labelled_field(page, "File #"):
        logger.warning("  %s: LegislationDetail returned no fields", file_number)
        return None
    sponsors_raw = labelled_field(page, "Sponsors")
    by_mayor = "by request of the mayor" in sponsors_raw.lower()
    sponsors = [s.strip() for s in re.split(r",(?![^(]*\))", sponsors_raw)
                if s.strip() and "by request" not in s.lower()]

    attachments = []
    for m in re.finditer(r'href="(View\.ashx\?M=F[^"]+)"[^>]*>(.*?)</a>', page, re.S):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(m.group(2)))).strip()
        if label:
            attachments.append({
                "label": label,
                "kind": classify_attachment(label),
                "url": f"{LEGISTAR_HOST}/{html.unescape(m.group(1))}",
            })

    history = []
    for row in table_rows(page):
        if len(row) >= 6 and re.match(r"\d{1,2}/\d{1,2}/\d{4}", row[0]["text"]):
            history.append({
                "date": _iso_date(row[0]["text"]),
                "body": row[3]["text"],
                "action": row[4]["text"],
                "result": row[5]["text"],
            })

    return {
        "file_number": file_number,
        "slug": matter_slug(file_number),
        "legistar_id": legistar_id,
        "type": labelled_field(page, "Type"),
        "title": labelled_field(page, "Title"),
        "status": labelled_field(page, "Status"),
        "sponsors": sponsors,
        "by_request_of_mayor": by_mayor,
        "attachments": attachments,
        "fiscal": None,
        "history": history,
        "legistar_url": (f"{LEGISTAR_HOST}/LegislationDetail.aspx"
                         f"?ID={legistar_id}&GUID={guid}"),
    }


# --- attachments and fiscal impact --------------------------------------


def classify_attachment(label):
    """Group an attachment by what it is, from Legistar's own label."""
    low = (label or "").lower()
    if "fiscal impact" in low:
        return "Fiscal Impact Statement"
    if "committee report" in low:
        return "Committee Report"
    if low.startswith("summary"):
        return "Summary"
    if "testimony" in low:
        return "Hearing Testimony"
    if "transcript" in low:
        return "Hearing Transcript"
    if "agenda" in low:
        return "Agenda"
    if "letter" in low or "message" in low:
        return "Message or letter"
    if re.match(r"(int|res|proposed|preconsidered)", low):
        return "Bill text"
    return "Other"


def attachment_text(data):
    """Plain text from a Legistar attachment, .docx or PDF.

    Most are Office Open XML (a zip) served as ``application/msword``, so the
    stdlib reads them and no new dependency is needed. A minority are PDFs --
    four of the 118 Fiscal Impact Statements in the archive -- and those go
    through pdfplumber, which the summarizer already depends on. Table cells
    are separated by ' | ' so the caller can read a row left to right.
    """
    if data[:4] == b"%PDF":
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = []
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        for row in table:
                            pages.append(" | ".join(c or "" for c in row))
                    pages.append(page.extract_text() or "")
            return re.sub(r"[ \t]+", " ", "\n".join(pages))
        except Exception as e:  # a malformed PDF should not stop the run
            logger.warning("  PDF attachment unreadable: %s", e)
            return ""
    if data[:2] != b"PK":
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", "", xml))


_MONEY = re.compile(r"\(?\s*\$\s*(-?[\d,]+(?:\.\d+)?)\s*\)?")


def _money_row(text, label):
    """The three figures on one Fiscal Impact row: effective, succeeding, full.

    The statement lays each row out as a label followed by three currency
    cells. A row that does not produce three figures (some say "See Below")
    returns None rather than a partly-guessed number.
    """
    m = re.search(rf"{label}[^|]*((?:\s*\|[^|]*){{1,8}})", text)
    if not m:
        return None
    figures = []
    for cell in m.group(1).split("|"):
        hit = _MONEY.search(cell)
        if hit:
            value = int(float(hit.group(1).replace(",", "")))
            if "(" in cell and ")" in cell:
                value = -value
            figures.append(value)
        if len(figures) == 3:
            break
    if len(figures) != 3:
        return None
    return {"effective": figures[0], "succeeding": figures[1], "full": figures[2]}


def parse_fiscal_impact(text):
    """Pull the City Council estimate out of a Fiscal Impact Statement."""
    if "fiscal impact statement" not in text.lower():
        return None
    out = {
        "revenues": _money_row(text, r"Revenues"),
        "expenditures": _money_row(text, r"Expenditures"),
        "net": _money_row(text, r"\bNet\b"),
    }
    fy = re.search(r"First Become Effective:\s*(\d{4})", text)
    out["effective_fy"] = fy.group(1) if fy else ""
    omb = re.search(r"Office of Management and Budget Estimate:\s*([^\n|]{0,120})", text)
    out["omb_estimate"] = omb.group(1).strip() if omb else ""
    out["omb_declined"] = bool(re.search(r"OMB did not provide", text, re.I))
    if not any((out["revenues"], out["expenditures"], out["net"])):
        return None
    return out


def add_fiscal_impact(fetcher, matter):
    """Attach the newest Fiscal Impact Statement's figures to a matter."""
    statements = [a for a in matter["attachments"]
                  if a["kind"] == "Fiscal Impact Statement"]
    if not statements:
        return False
    doc = statements[-1]
    try:
        parsed = parse_fiscal_impact(attachment_text(fetcher.get_bytes(doc["url"])))
    except requests.HTTPError as e:
        logger.warning("  %s fiscal: %s", matter["file_number"], e)
        return False
    if not parsed:
        return False
    parsed["label"] = doc["label"]
    parsed["source"] = doc["url"]
    matter["fiscal"] = parsed
    return True


# --- main ---------------------------------------------------------------


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", default="", help="only meetings on/after this ISO date")
    p.add_argument("--limit", type=int, default=0, help="stop after N meetings")
    p.add_argument("--dry-run", action="store_true", help="scrape but write nothing")
    p.add_argument("--no-cache", action="store_true", help="ignore the HTML cache")
    p.add_argument("--skip-fiscal", action="store_true",
                   help="skip downloading Fiscal Impact Statements")
    p.add_argument("--skip-votes", action="store_true",
                   help="skip roll calls (much faster; use to sanity-check the grid)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    known = {**calendar_meetings(), **archive_meetings()}
    today = time.strftime("%Y-%m-%d")

    # Filter on the dates we already hold, before fetching anything. Future
    # meetings have nothing to record yet.
    meetings = [(eid, guid, date) for eid, (guid, date) in known.items()
                if date and date <= today and (not args.since or date >= args.since)]
    meetings.sort(key=lambda m: (m[2], m[0]))
    logger.info("Meetings known: %d; in scope after date filter: %d",
                len(known), len(meetings))

    fetcher = Fetcher(use_cache=not args.no_cache)
    matters_seen = {}
    written = 0

    for event_id, guid, _date in meetings:
        if args.limit and written >= args.limit:
            break
        try:
            record = scrape_meeting(fetcher, event_id, guid,
                                    with_votes=not args.skip_votes)
        except requests.HTTPError as e:
            logger.warning("  %s: %s", event_id, e)
            continue
        if not record:
            continue

        voted = sum(1 for i in record["items"] if i.get("roll_call"))
        logger.info("%s | %s | %-52s %3d items, %2d with a roll call",
                    record["date"], event_id, record["body"][:52],
                    len(record["items"]), voted)

        if not args.dry_run:
            write_json(DATA_DIR / "meetings" / f"{event_id}.json", record)
        written += 1

        for item in record["items"]:
            if item["legistar_id"] and item["legistar_guid"]:
                matters_seen.setdefault(
                    item["legistar_id"], (item["legistar_guid"], item["file_number"]))

    logger.info("Meetings written: %d", written)
    logger.info("Distinct matters referenced: %d", len(matters_seen))

    matters_written = 0
    fiscal_found = 0
    for legistar_id, (guid, file_number) in sorted(matters_seen.items()):
        try:
            matter = scrape_matter(fetcher, legistar_id, guid, file_number)
        except requests.HTTPError as e:
            logger.warning("  matter %s: %s", file_number, e)
            continue
        if not matter:
            continue
        if not args.skip_fiscal and add_fiscal_impact(fetcher, matter):
            fiscal_found += 1
        if not args.dry_run:
            write_json(DATA_DIR / "matters" / f"{matter['slug']}.json", matter)
        matters_written += 1
    logger.info("Matters written: %d (%d with fiscal impact figures)",
                matters_written, fiscal_found)

    logger.info("Done. HTTP fetched %d, served from cache %d.",
                fetcher.fetched, fetcher.cached)


if __name__ == "__main__":
    main()
