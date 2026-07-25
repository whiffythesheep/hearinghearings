"""Scrape council.nyc.gov for the definitive Council roster.

Writes `council_roster.json` at the repo root: every district and its
sitting member, plus every committee/subcommittee with its chair and
membership. The summarizer validates transcript names against this file
the same way it applies `word_bank.json` corrections — see
`validate_member_names()` in summarize_council_meeting.py.

Run this whenever the Council's membership changes (special elections,
resignations, committee reshuffles):

    python refresh_council_roster.py

Sources (both public, no auth):
    https://council.nyc.gov/districts/    district number -> member
    https://council.nyc.gov/committees/   committee -> page URL
    https://council.nyc.gov/committees/<slug>/   -> members + (Chair)

Why this exists: the Viebit/YouTube caption cleaning pass will resolve a
garbled surname into a *plausible but wrong* council member, including
members who have left office. A checked-in roster gives that check a
ground truth that does not depend on whichever names happen to appear in
the archive already.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

from council_scraper import BROWSER_HEADERS

COUNCIL_HOST = "https://council.nyc.gov"
DISTRICTS_URL = f"{COUNCIL_HOST}/districts/"
COMMITTEES_URL = f"{COUNCIL_HOST}/committees/"
ROSTER_PATH = Path(__file__).parent / "council_roster.json"

# Politeness delay between committee page fetches.
FETCH_DELAY_SECONDS = 0.3

logger = logging.getLogger(__name__)


def _get(url: str) -> str:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _clean(s: str) -> str:
    """Unescape entities and collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# The districts page prefixes leadership roles onto the member's name
# ("Speaker Julie Menin"). Keep the role, but store the bare name so
# name matching does not have to know about titles.
LEADERSHIP_RE = re.compile(
    r"^(Speaker|Deputy Speaker|Majority Leader|Minority Leader|"
    r"Majority Whip|Minority Whip|Assistant Majority Leader|"
    r"Assistant Minority Leader)\s+",
    re.IGNORECASE,
)
HONORIFIC_RE = re.compile(r"^(Dr|Rev|Hon|Mr|Ms|Mrs)\.?\s+", re.IGNORECASE)


def split_title(raw: str) -> tuple[str, str | None]:
    """('Deputy Speaker Dr. Nantasha Williams') -> (name, role)."""
    role_m = LEADERSHIP_RE.match(raw)
    role = role_m.group(1) if role_m else None
    name = LEADERSHIP_RE.sub("", raw)
    name = HONORIFIC_RE.sub("", name)
    return name.strip(), role


def scrape_districts() -> dict[str, str]:
    """Return {district_number: member_name} for all 51 districts."""
    text = _get(DISTRICTS_URL)
    # Each row pairs a sort-district cell with a sort-member cell whose
    # anchor carries a clean data-member-name attribute.
    pattern = re.compile(
        r'<td class="sort-district">.*?<strong>(\d+)</strong>.*?'
        r'<td class="sort-member">.*?data-member-name="([^"]+)"',
        re.DOTALL,
    )
    districts = {}
    for m in pattern.finditer(text):
        name, role = split_title(_clean(m.group(2)))
        districts[m.group(1)] = {"name": name, "role": role}
    if len(districts) < 45:
        raise RuntimeError(
            f"Only parsed {len(districts)} districts from {DISTRICTS_URL} — "
            "the page markup likely changed."
        )
    return dict(sorted(districts.items(), key=lambda kv: int(kv[0])))


def scrape_committee_index() -> dict[str, str]:
    """Return {committee_name: page_url} for committees + subcommittees."""
    text = _get(COMMITTEES_URL)
    # Narrow to the committee list so we don't pick up nav links.
    start = text.find('id="committee-list"')
    if start == -1:
        raise RuntimeError("Could not find #committee-list on the page.")
    end = text.find("</div>", start)
    block = text[start:end]
    pattern = re.compile(
        r'<a href="(https://council\.nyc\.gov/committees/[^"]+)">([^<]+)</a>'
    )
    out = {}
    for url, name in pattern.findall(block):
        out[_clean(name)] = url
    if not out:
        raise RuntimeError("Parsed zero committees — markup likely changed.")
    return dict(sorted(out.items()))


def scrape_committee(url: str) -> dict:
    """Return {'chair': str|None, 'members': [str]} for one committee."""
    text = _get(url)
    start = text.find('id="committee-members"')
    if start == -1:
        return {"chair": None, "members": []}
    end = text.find("</ul>", start)
    block = text[start:end]

    members, chair = [], None
    for li in re.findall(r"<li>(.*?)</li>", block, re.DOTALL):
        name_m = re.search(r"<strong>(.*?)</strong>", li, re.DOTALL)
        if not name_m:
            continue
        name, _ = split_title(_clean(re.sub(r"<[^>]+>", "", name_m.group(1))))
        if not name:
            continue
        members.append(name)
        # The chair is flagged with a trailing <small>(Chair)</small>.
        if re.search(r"\(\s*Chair\s*\)", li, re.IGNORECASE):
            chair = name
    return {"chair": chair, "members": members}


def build_roster() -> dict:
    logger.info("Scraping districts")
    districts = scrape_districts()
    logger.info("  %d districts", len(districts))

    logger.info("Scraping committee index")
    index = scrape_committee_index()
    logger.info("  %d committees/subcommittees", len(index))

    committees = {}
    for i, (name, url) in enumerate(index.items(), 1):
        logger.info("  [%d/%d] %s", i, len(index), name)
        try:
            info = scrape_committee(url)
        except requests.RequestException as exc:
            logger.warning("    fetch failed (%s) — skipping", exc)
            continue
        info["url"] = url
        committees[name] = info
        time.sleep(FETCH_DELAY_SECONDS)

    # Union of district members and everyone seen on a committee, so the
    # validator recognises members however they are written.
    names = {d["name"] for d in districts.values()}
    for info in committees.values():
        names.update(info["members"])

    return {
        "scraped_at": date.today().isoformat(),
        "sources": {
            "districts": DISTRICTS_URL,
            "committees": COMMITTEES_URL,
        },
        "districts": districts,
        "members": sorted(names),
        "committees": committees,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    roster = build_roster()

    previous = {}
    if ROSTER_PATH.exists():
        previous = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))

    with open(ROSTER_PATH, "w", encoding="utf-8") as f:
        json.dump(roster, f, indent=2, ensure_ascii=False)
        f.write("\n")

    logger.info("Wrote %s", ROSTER_PATH.name)
    logger.info(
        "  %d districts, %d committees, %d distinct members",
        len(roster["districts"]), len(roster["committees"]),
        len(roster["members"]),
    )

    # Report membership churn so a special election is visible in the diff.
    if previous:
        old, new = previous.get("districts", {}), roster["districts"]
        def member_of(entry):
            # Tolerate the pre-2026-07 shape, where districts mapped
            # straight to a name string rather than {name, role}.
            if isinstance(entry, str):
                return entry
            return (entry or {}).get("name", "—")

        for d in sorted(set(old) | set(new), key=int):
            a = member_of(old.get(d))
            b = member_of(new.get(d))
            if a != b:
                logger.info("  CHANGED District %s: %s -> %s", d, a, b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
