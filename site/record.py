"""Load the record layer written by ``scrape_record.py`` into view models.

``data/`` holds two kinds of scraped record -- meetings and matters. Members
and committees are *derived* here at build time rather than stored, so they
cannot drift out of step with the votes they summarise.

The one join that needs care is member identity. Legistar writes roll-call
names with middle initials and diacritics ("Elsie Encarnación", "Althea V.
Stevens") while ``council_roster.json`` often does not ("Elsie Encarnacion",
"Althea Stevens") -- and the initial sometimes sits on the roster side
instead ("Kamillah M. Hanks" against Legistar's "Kamillah Hanks"). Folding
both ways matches all 51 members exactly; see ``normalize_name``.
"""

import json
import os
import re
import unicodedata
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ROSTER_PATH = os.path.join(ROOT, "council_roster.json")

# Vote values Legistar uses that mean "cast a vote" rather than "was away".
CAST_VOTES = ("Affirmative", "Negative", "Abstain")


def slugify(text):
    s = re.sub(r"[^\w\s-]", "", (text or "").lower())
    return re.sub(r"[-\s]+", "-", s).strip("-")


def normalize_name(name):
    """Fold a member name to a comparison key.

    Strips diacritics and any middle initial, so the Legistar and roster
    spellings of one person collapse to the same key.
    """
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    n = re.sub(r"\b[A-Z]\.\s*", " ", n)
    return re.sub(r"\s+", " ", n).strip().lower()


def display_date(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (ValueError, TypeError):
        return iso or ""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _read_dir(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.isdir(path):
        return []
    out = []
    for fn in sorted(os.listdir(path)):
        if fn.endswith(".json"):
            with open(os.path.join(path, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _load_roster():
    if not os.path.exists(ROSTER_PATH):
        return {}, {}
    with open(ROSTER_PATH, encoding="utf-8") as f:
        roster = json.load(f)
    by_name = {}
    for district, entry in roster.get("districts", {}).items():
        by_name[normalize_name(entry.get("name", ""))] = {
            "district": district,
            "roster_name": entry.get("name", ""),
        }
    return by_name, roster.get("committees", {})


def load_records(hearings):
    """Return meetings, matters, members and committees for the templates.

    ``hearings`` is build.py's loaded content, used only to link a scraped
    meeting back to its published page where one exists.
    """
    meetings = _read_dir("meetings")
    matters = _read_dir("matters")
    roster_by_name, roster_committees = _load_roster()

    # event id -> published hearing, via the council_url in front matter
    hearing_by_event = {}
    for h in hearings:
        m = re.search(r"MeetingDetail\.aspx\?ID=(\d+)", h.get("council_url", "") or "")
        if m:
            hearing_by_event[m.group(1)] = h

    matter_by_slug = {m["slug"]: m for m in matters}
    for m in matters:
        m["appearances"] = []
        m["display_status"] = m.get("status") or "—"
        m["short_title"] = (m.get("name") or m.get("title") or "").strip()

    members = {}
    committees = {}

    for meeting in sorted(meetings, key=lambda x: (x.get("date", ""), x.get("event_id"))):
        hearing = hearing_by_event.get(meeting["event_id"])
        meeting["hearing_slug"] = hearing["slug"] if hearing else ""
        meeting["hearing_title"] = hearing["title"] if hearing else ""
        meeting["date_display"] = display_date(meeting.get("date", ""))

        body = meeting.get("body", "").strip()
        if body:
            c = committees.setdefault(body, {
                "name": body, "slug": slugify(body), "meetings": [],
                "matter_slugs": set(), "vote_count": 0,
            })
            c["meetings"].append(meeting)

        for item in meeting.get("items", []):
            slug = item.get("file_number") and slugify(item["file_number"])
            matter = matter_by_slug.get(slug)
            item["matter_slug"] = slug if matter is not None else ""
            if matter is not None:
                matter["appearances"].append({
                    "date": meeting.get("date", ""),
                    "date_display": meeting["date_display"],
                    "body": body,
                    "event_id": meeting["event_id"],
                    "hearing_slug": meeting["hearing_slug"],
                    "hearing_title": meeting["hearing_title"],
                    "action": item.get("action", ""),
                    "result": item.get("result", ""),
                    "tally": item.get("tally") or {},
                })
            if body and slug:
                committees[body]["matter_slugs"].add(slug)

            for vote in item.get("roll_call") or []:
                key = normalize_name(vote["name"])
                person = members.setdefault(key, {
                    "name": vote["name"], "slug": slugify(normalize_name(vote["name"])),
                    "district": "", "votes": [], "tally": {}, "committees": [],
                })
                # Legistar's spelling is the fuller one; prefer it for display.
                if len(vote["name"]) > len(person["name"]):
                    person["name"] = vote["name"]
                person["votes"].append({
                    "date": meeting.get("date", ""),
                    "date_display": meeting["date_display"],
                    "body": body,
                    "hearing_slug": meeting["hearing_slug"],
                    "file_number": item.get("file_number", ""),
                    "matter_slug": slug,
                    "matter_title": (matter or {}).get("short_title", ""),
                    "action": item.get("action", ""),
                    "result": item.get("result", ""),
                    "vote": vote["vote"],
                })
                person["tally"][vote["vote"]] = person["tally"].get(vote["vote"], 0) + 1
                if body:
                    committees[body]["vote_count"] += 1

    # --- finish members -------------------------------------------------
    for key, person in members.items():
        info = roster_by_name.get(key, {})
        person["district"] = info.get("district", "")
        person["votes"].sort(key=lambda v: (v["date"], v["file_number"]), reverse=True)
        cast = sum(person["tally"].get(v, 0) for v in CAST_VOTES)
        away = sum(n for v, n in person["tally"].items() if v not in CAST_VOTES)
        person["votes_cast"] = cast
        person["times_away"] = away
        person["attendance_pct"] = round(100 * cast / (cast + away)) if cast + away else 0
        person["dissents"] = person["tally"].get("Negative", 0)
    for name, entry in roster_committees.items():
        for member_name in entry.get("members", []):
            person = members.get(normalize_name(member_name))
            if person is not None and name not in person["committees"]:
                person["committees"].append(name)
    member_list = sorted(members.values(), key=lambda p: p["name"].split()[-1].lower())

    # --- finish committees ----------------------------------------------
    roster_chair = {normalize_name(n): e.get("chair", "")
                    for n, e in roster_committees.items()}
    committee_list = []
    for name, c in committees.items():
        c["chair"] = roster_chair.get(normalize_name(name), "")
        c["meetings"].sort(key=lambda m: m.get("date", ""), reverse=True)
        c["matters"] = sorted(
            (matter_by_slug[s] for s in c["matter_slugs"] if s in matter_by_slug),
            key=lambda m: m["file_number"],
        )
        del c["matter_slugs"]
        c["meeting_count"] = len(c["meetings"])
        committee_list.append(c)
    committee_list.sort(key=lambda c: c["name"])

    # --- finish matters -------------------------------------------------
    for m in matters:
        m["appearances"].sort(key=lambda a: a["date"], reverse=True)
        m["last_action"] = m["appearances"][0] if m["appearances"] else None
        m["latest_date"] = m["appearances"][0]["date"] if m["appearances"] else ""
    matters.sort(key=lambda m: (m["latest_date"], m["file_number"]), reverse=True)

    # --- finish meetings ------------------------------------------------
    # Every meeting gets a row in the chronological index. Only those with
    # no published hearing get a page of their own -- for the rest the
    # hearing page already is the page, and a second one would duplicate it.
    for meeting in meetings:
        meeting["slug"] = meeting["event_id"]
        meeting["has_hearing"] = bool(meeting["hearing_slug"])
        meeting["link"] = (f"/hearings/{meeting['hearing_slug']}/"
                           if meeting["has_hearing"]
                           else f"/meetings/{meeting['event_id']}/")
        meeting["title"] = meeting["hearing_title"] or meeting.get("body", "")
        meeting["voted_items"] = [i for i in meeting.get("items", [])
                                  if i.get("roll_call")]
        meeting["kind"] = ("Hearing" if meeting["has_hearing"]
                           else ("Stated" if "stated" in meeting.get("body", "").lower()
                                 or meeting.get("body", "") == "City Council"
                                 else "Vote"))
    meeting_list = sorted(meetings, key=lambda m: (m.get("date", ""), m["event_id"]),
                          reverse=True)

    return {
        "meetings": {m["event_id"]: m for m in meetings},
        "meeting_list": meeting_list,
        "record_only_meetings": [m for m in meeting_list if not m["has_hearing"]],
        "matters": matters,
        "matter_by_slug": matter_by_slug,
        "members": member_list,
        "committees": committee_list,
    }


# --- linking matter numbers in prose ------------------------------------

# "Int 0892-2026", "Res 0595-2026", "LU 0120-2026", "T2026-2501".
MATTER_RE = re.compile(
    r"\b(?:(Int|Res|LU|M|Res\.)\s?(?:No\.\s?)?(\d{1,4}-\d{4})|T(\d{4}-\d{4}))\b"
)


def link_matter_numbers(html, matter_by_slug):
    """Turn matter numbers in rendered prose into links.

    Only exact file numbers are linked, and only when that matter exists in
    ``data/``. Member names are deliberately NOT linked from prose: the
    transcript spellings are caption-derived and a wrong link would point at
    a real person who was not there.
    """
    if not html:
        return html

    def repl(match):
        if match.group(3):
            slug = slugify(f"T{match.group(3)}")
            label = match.group(0)
        else:
            prefix = match.group(1).rstrip(".")
            slug = slugify(f"{prefix} {match.group(2)}")
            label = match.group(0)
        if slug not in matter_by_slug:
            return label
        return f'<a class="matter-ref" href="/matters/{slug}/">{label}</a>'

    # Don't rewrite inside an existing anchor or tag attribute.
    parts = re.split(r"(<[^>]+>)", html)
    for i, part in enumerate(parts):
        if not part.startswith("<"):
            parts[i] = MATTER_RE.sub(repl, part)
    return "".join(parts)
