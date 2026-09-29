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
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ROSTER_PATH = os.path.join(ROOT, "council_roster.json")

# Vote values Legistar uses that mean "cast a vote" rather than "was away".
CAST_VOTES = ("Affirmative", "Negative", "Abstain")


# Matter types left out of the site: procedural paperwork rather than
# decisions (budget transmittals, appointment nominations, clerk's notices,
# the Commissioner of Deeds list, the "such other business" agenda line).
EXCLUDED_TYPES = {"Communication", "Mayor's Message", "Commissioner of Deeds", "N/A"}

# Legistar's action wording, in plain English. "P-C" (pre-considered) items
# are bills heard before they are formally introduced.
PLAIN_ACTIONS = {
    "Introduced by Council": "Introduced",
    "Referred to Comm by Council": "Sent to committee",
    "Re-referred to Committee by Council": "Sent back to committee",
    "Hearing Held by Committee": "Heard in committee",
    "Hearing on P-C Item by Comm": "Heard in committee before introduction",
    "Laid Over by Committee": "Held over by committee",
    "Laid Over by Subcommittee": "Held over by subcommittee",
    "P-C Item Laid Over by Comm": "Held over by committee before introduction",
    "Amendment Proposed by Comm": "Amended in committee",
    "Amended by Committee": "Amended in committee",
    "Approved by Committee": "Passed committee",
    "Approved by Committee with Companion Resolution": "Passed committee",
    "P-C Item Approved by Comm": "Passed committee",
    "P-C Item Approved by Committee with Companion Resolution": "Passed committee",
    "Approved by Subcommittee": "Passed subcommittee",
    "Approved by Committee with Modifications and Referred to CPC":
        "Passed committee with changes, sent back to City Planning",
    "Approved by Subcommittee with Modifications and Referred to CPC":
        "Passed subcommittee with changes, sent back to City Planning",
    "Disapproved by Committee": "Rejected by committee",
    "Disapproved by Committee with Companion Resolution": "Rejected by committee",
    "Disapproved by Subcommittee": "Rejected by subcommittee",
    "Filed, by Committee": "Closed by committee",
    "Filed by Committee": "Closed by committee",
    "Filed by Subcommittee": "Closed by subcommittee",
    "Filed by Council": "Closed by the Council",
    "Rcvd, Ord, Prnt, Fld by Council": "Received and closed by the Council",
    "Deferred": "Postponed",
    "Withdrawn": "Withdrawn",
    "Approved, by Council": "Passed by the full Council",
    "Approved by Council": "Passed by the full Council",
    "Approved with Modifications and Referred to the City Planning Commission "
    "pursuant to Section 197-(d) of the New York City Charter.":
        "Passed by the full Council with changes, sent back to City Planning",
    "Sent to Mayor by Council": "Sent to the Mayor",
    "Hearing Held by Mayor": "Mayor's public hearing held",
    "Signed Into Law by Mayor": "Signed into law by the Mayor",
    "Recved from Mayor by Council": "Returned by the Mayor",
    "Returned Unsigned by Mayor": "Returned by the Mayor unsigned",
    "City Charter Rule Adopted": "Became law without the Mayor's signature",
}


def plain_action(action):
    if not action:
        return ""
    return PLAIN_ACTIONS.get(action.strip(), action)


# Where a matter stands, in the order a matter moves through. Oversight
# topics are hearings, not decisions, so they sit outside the pipeline.
STAGES = ["Introduced", "In committee", "Passed committee",
          "Awaiting the Mayor", "Law or adopted", "Closed"]

_STAGE_BY_STATUS = {
    "Introduced": "Introduced",
    "Committee": "In committee",
    "Laid Over in Committee": "In committee",
    "Companion Pending Approval by Council": "Passed committee",
    "Enacted (Mayor's Desk for Signature)": "Awaiting the Mayor",
    "Enacted": "Law or adopted",
    "Adopted": "Law or adopted",
    "Filed": "Closed",
    "Withdrawn": "Closed",
    "Disapproved": "Closed",
    "Vetoed": "Closed",
    "Received, Ordered, Printed and Filed": "Closed",
}


def matter_stage(matter):
    if matter.get("type") == "Oversight":
        return "Oversight topic"
    status = matter.get("status") or ""
    stage = _STAGE_BY_STATUS.get(status, "")
    # Legistar leaves status at "Committee" between a committee vote and the
    # Council vote; the history tells them apart.
    history = matter.get("history") or []
    if stage == "In committee" and history:
        latest_day = [h for h in history if h.get("date") == history[0].get("date")]
        if any(plain_action(h.get("action")).startswith("Passed") for h in latest_day):
            stage = "Passed committee"
    return stage or status or "—"


def neighbourhood_summary(hoods, limit=3):
    """"Financial District-Battery Park City, Tribeca-Civic Center, ..." ->
    "Financial District, Tribeca, SoHo". Parks, cemeteries and islands only
    count if nothing else is left."""
    # The source splits on every comma, including inside "(Lenox Hill, Yorkville)".
    merged = []
    for h in hoods:
        if merged and merged[-1].count("(") > merged[-1].count(")"):
            merged[-1] += ", " + h
        else:
            merged.append(h)
    hoods = merged
    heads = [re.sub(r"\s*\(.*?\)", "", h.split("-")[0]).split(" and parts of")[0].strip()
             for h in hoods]
    places = [h for h in heads
              if not re.search(r"\b(Park|Cemetery|Airport|Island|Beach)\b", h)] or heads
    seen = []
    for p in places:
        if p not in seen:
            seen.append(p)
    return ", ".join(seen[:limit])


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


# How much an action says about where a matter stands. When one matter has
# several rows at the same meeting (heard, amended, approved), only the most
# decisive is shown: a vote implies the discussion that preceded it.
_DECISIVE = re.compile(r"approv|adopt|disapprov|filed|withdrawn|veto|sign|enact|"
                       r"returned|overrid|fail|pass", re.I)
_PROCEDURAL = re.compile(r"laid over|deferred|referred|sent to|rcvd", re.I)


def action_rank(entry):
    action = entry.get("action") or ""
    if entry.get("result") or entry.get("tally") or entry.get("roll_call"):
        return 4
    if _DECISIVE.search(action):
        return 3
    if action.lower().startswith("introduced"):
        return 2.5
    if _PROCEDURAL.search(action):
        return 2
    return 1 if action else 0


def collapse(entries, key):
    """Collapse entries sharing ``key`` to the single most decisive one.

    Keeps first-seen order. Roll calls from every collapsed row are kept on
    the survivor, so no recorded vote is lost.
    """
    groups = {}
    order = []
    for e in entries:
        k = key(e)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(e)
    out = []
    for k in order:
        group = groups[k]
        best = max(group, key=action_rank)  # max() keeps the first on ties
        row = dict(best)
        row["name"] = next((g["name"] for g in group if g.get("name")), row.get("name", ""))
        row["roll_calls"] = [g for g in group if g.get("roll_call")]
        out.append(row)
    return out


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
            "party": entry.get("party", ""),
            "borough": entry.get("borough", ""),
            "area": neighbourhood_summary(entry.get("neighborhoods") or []),
        }
    return by_name, roster.get("committees", {})


def _norm_body(name):
    n = (name or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


# "(L.U. No. 119)" in a resolution's title names the Land Use application it
# disposes of. Legistar files the committee vote under the LU number and the
# Council vote under both, so the two need joining to tell one story.
LU_REF_RE = re.compile(r"L\.U\. No\. (\d+)")
# Some resolutions cite the ULURP application number instead ("C 260089 PCQ").
ULURP_RE = re.compile(r"\b([CNGD] \d{6,10} [A-Z]{3,4})\b")


def _link_companions(matters, matter_by_slug):
    for m in matters:
        m.setdefault("companions", [])
    lu_by_ulurp = {}
    for m in matters:
        if m["file_number"].startswith("LU"):
            text = f"{m.get('name') or ''} {m.get('title') or ''}"
            for code in set(ULURP_RE.findall(text)):
                lu_by_ulurp.setdefault(code, m)
    for m in matters:
        if not m["file_number"].startswith("Res"):
            continue
        title = m.get("title") or ""
        ref = LU_REF_RE.search(title)
        lu = None
        if ref:
            year = m["file_number"].rsplit("-", 1)[-1]
            lu = matter_by_slug.get(f"lu-{int(ref.group(1)):04d}-{year}")
        elif title.startswith("Application number"):
            code = ULURP_RE.search(title)
            lu = lu_by_ulurp.get(code.group(1)) if code else None
        if lu is None:
            continue
        if lu["slug"] not in m["companions"]:
            m["companions"].append(lu["slug"])
        if m["slug"] not in lu["companions"]:
            lu["companions"].append(m["slug"])


def _record_only_title(meeting):
    """A descriptive title for a meeting with no published summary."""
    body = meeting.get("body", "")
    if body == "City Council":
        return "Stated Meeting"
    rows = meeting["rows"]
    voted = [r for r in rows if r.get("roll_calls")]
    if voted:
        first = (voted[0].get("name") or voted[0]["file_number"]).rstrip(".")
        more = len(voted) - 1
        return f"Vote: {first}" + (f" + {more} more" if more else "")
    oversight = next((r for r in rows if r.get("type") == "Oversight"), None)
    if oversight:
        return re.sub(r"^Oversight\s*[-–—:]\s*", "", oversight.get("name", "")).strip()
    if rows:
        first = rows[0].get("name") or rows[0]["file_number"]
        more = len(rows) - 1
        return first + (f" + {more} more" if more else "")
    return body


def load_records(hearings, today=None):
    """Return meetings, matters, members and committees for the templates.

    ``hearings`` is build.py's loaded content, used to link a scraped meeting
    back to its published page where one exists.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    # Only the next day's agendas are shown; anything later waits.
    # Deferred meetings never took place (Legistar keeps the event, with the
    # agenda status and time both reading "Deferred"), so they are left out.
    meetings = [mt for mt in _read_dir("meetings")
                if mt.get("date", "") <= tomorrow and mt.get("agenda_status") != "Deferred"]
    matters = [m for m in _read_dir("matters") if m.get("type") not in EXCLUDED_TYPES]
    for meeting in meetings:
        meeting["items"] = [i for i in meeting.get("items", [])
                            if i.get("type") not in EXCLUDED_TYPES]
    roster_by_name, roster_committees = _load_roster()

    # event id -> published hearing, via the council_url in front matter
    hearing_by_event = {}
    # (date, committee) -> hearing: a joint hearing is one Legistar event per
    # committee, but only the lead one is in our front matter. The siblings
    # belong to the same published page.
    hearing_by_day_body = {}
    for h in hearings:
        m = re.search(r"MeetingDetail\.aspx\?ID=(\d+)", h.get("council_url", "") or "")
        if m:
            hearing_by_event[m.group(1)] = h
        for c in h.get("committee_list", []):
            hearing_by_day_body.setdefault((h["date"], _norm_body(c)), h)

    matter_by_slug = {m["slug"]: m for m in matters}
    # Plain-English titles and summaries from summarize_matters.py, where made.
    plain = {p.get("file_number"): p for p in _read_dir("matter_plain")}
    for m in matters:
        m["appearances"] = []
        m["official_title"] = (m.get("title") or m.get("name") or "").strip()
        p = plain.get(m["file_number"]) or {}
        m["plain_title"] = p.get("title", "")
        m["plain_summary"] = p.get("summary", "")
        m["display_status"] = m.get("status") or "—"
        m["stage"] = matter_stage(m)
        m["short_title"] = m["plain_title"] or (m.get("name") or m.get("title") or "").strip()
    _link_companions(matters, matter_by_slug)

    members = {}
    committees = {}
    meeting_by_day_body = {}

    for meeting in sorted(meetings, key=lambda x: (x.get("date", ""), x.get("event_id"))):
        body = meeting.get("body", "").strip()
        hearing = (hearing_by_event.get(meeting["event_id"])
                   or hearing_by_day_body.get((meeting.get("date", ""), _norm_body(body))))
        meeting["hearing_slug"] = hearing["slug"] if hearing else ""
        meeting["hearing_title"] = hearing["title"] if hearing else ""
        meeting["date_display"] = display_date(meeting.get("date", ""))
        meeting_by_day_body[(meeting.get("date", ""), _norm_body(body))] = meeting

        # One row per matter: Legistar lists "Hearing Held", "Amended" and
        # "Approved" separately, and Stated Meetings list every General
        # Orders item a second time with no action.
        rows = collapse(meeting.get("items", []),
                        key=lambda i: i.get("file_number") or id(i))
        for row in rows:
            slug = row.get("file_number") and slugify(row["file_number"])
            matter = matter_by_slug.get(slug)
            row["matter_slug"] = slug if matter is not None else ""
            # One title per matter everywhere on the site; Legistar's agenda
            # wording survives as a tooltip.
            row["official_name"] = row.get("name", "")
            if matter is not None and matter["plain_title"]:
                row["name"] = matter["plain_title"]
            row["companions"] = [matter_by_slug[c]
                                 for c in (matter or {}).get("companions", [])]
        # A resolution voted alongside its own LU application is one decision:
        # show it as "with Res …" on the application's row, not a second row.
        present = {r["matter_slug"] for r in rows if r["matter_slug"]}
        rows = [r for r in rows
                if not (r.get("file_number", "").startswith("Res")
                        and any(c["slug"] in present for c in r["companions"]))]
        meeting["rows"] = rows

        if body:
            c = committees.setdefault(body, {
                "name": body, "slug": slugify(body), "meetings": [],
                "matter_slugs": set(), "vote_count": 0,
            })
            c["meetings"].append(meeting)
            for row in rows:
                if row["matter_slug"]:
                    c["matter_slugs"].add(row["matter_slug"])

        for item in meeting.get("items", []):
            slug = item.get("file_number") and slugify(item["file_number"])
            matter = matter_by_slug.get(slug)
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
                    "meeting": meeting,  # swapped for its link once links are known
                    "file_number": item.get("file_number", ""),
                    "matter_slug": slug if matter is not None else "",
                    "matter_title": (matter or {}).get("short_title", ""),
                    "action": item.get("action", ""),
                    "result": item.get("result", ""),
                    "vote": vote["vote"],
                })
                person["tally"][vote["vote"]] = person["tally"].get(vote["vote"], 0) + 1
                if body:
                    committees[body]["vote_count"] += 1

    # --- finish meetings ------------------------------------------------
    # Every meeting is listed alongside the hearings. Those with no published
    # hearing get a record-only page; the rest link to the hearing page.
    for meeting in meetings:
        meeting["slug"] = meeting["event_id"]
        meeting["has_hearing"] = bool(meeting["hearing_slug"])
        meeting["link"] = (f"/hearings/{meeting['hearing_slug']}/"
                           if meeting["has_hearing"]
                           else f"/meetings/{meeting['event_id']}/")
        meeting["voted_items"] = [r for r in meeting["rows"] if r.get("roll_calls")]
        acted = any(r.get("action") for r in meeting["rows"])
        if meeting["has_hearing"]:
            kind = "Summarised"
        elif meeting.get("date", "") >= today and not acted:
            kind = "Upcoming"
        elif meeting["voted_items"]:
            kind = "Vote session"
        else:
            # Held, but unsummarised and nothing voted: only process (items
            # held over or deferred). Not listed anywhere.
            kind = "Process only"
        meeting["kind"] = kind
        meeting["title"] = meeting["hearing_title"] or _record_only_title(meeting)
        meeting["listed"] = bool(meeting["rows"] or meeting["has_hearing"]) and kind != "Process only"
        meeting["bodies"] = [meeting.get("body", "")]

    # Joint meetings with no published hearing: Legistar gives each committee
    # its own event for the same session. List them once, under every body.
    primary_by_agenda = {}
    for meeting in sorted(meetings, key=lambda x: x["event_id"]):
        if meeting["has_hearing"] or not meeting["listed"]:
            continue
        # Oversight topics get a T-number per committee, so match on the
        # derived title (same day, same topic) rather than on file numbers.
        key = (meeting.get("date", ""), meeting["title"])
        primary = primary_by_agenda.setdefault(key, meeting)
        if primary is meeting:
            continue
        primary["bodies"].append(meeting.get("body", ""))
        meeting["listed"] = False
        meeting["sibling_of"] = primary
        meeting["link"] = primary["link"]
        meeting["title"] = primary["title"]
    # Whether anything on the site can link to a meeting: its own row, or
    # the row of the joint meeting it belongs to.
    for meeting in meetings:
        meeting["linkable"] = meeting["listed"] or "sibling_of" in meeting
    meeting_list = sorted(meetings, key=lambda m: (m.get("date", ""), m["event_id"]),
                          reverse=True)

    # --- finish matters -------------------------------------------------
    # The timeline is Legistar's own history (it covers meetings before the
    # archive and Council steps with no meeting of their own), one row per
    # body per day, linked to our page for that meeting where we have one.
    rows_by_matter = {}
    for meeting in meetings:
        for row in meeting["rows"]:
            if row["matter_slug"]:
                rows_by_matter.setdefault(row["matter_slug"], []).append((meeting, row))
    for m in matters:
        entries = [{"date": h.get("date", ""), "body": h.get("body", ""),
                    "action": h.get("action", ""), "result": h.get("result", "")}
                   for h in m.get("history") or []]
        timeline = collapse(entries, key=lambda e: (e["date"], _norm_body(e["body"])))
        seen = {(e["date"], _norm_body(e["body"])) for e in timeline}
        # A meeting in data/ that the history does not mention still counts.
        for meeting, row in rows_by_matter.get(m["slug"], []):
            k = (meeting.get("date", ""), _norm_body(meeting.get("body", "")))
            if k not in seen:
                seen.add(k)
                timeline.append({"date": k[0], "body": meeting.get("body", ""),
                                 "action": row.get("action", ""),
                                 "result": row.get("result", "")})
        slugs = {m["slug"], *m.get("companions", [])}
        for e in timeline:
            meeting = meeting_by_day_body.get((e["date"], _norm_body(e["body"])))
            e["date_display"] = display_date(e["date"])
            e["link"] = meeting["link"] if meeting and meeting["linkable"] else ""
            e["tally"] = {}
            if meeting:
                for row in meeting["rows"]:
                    if row["matter_slug"] in slugs and row.get("tally"):
                        e["tally"] = row["tally"]
                        break
            e["joint"] = bool(meeting and "jointly" in (meeting.get("location") or "").lower())
            # Scheduled but not yet held: an upcoming meeting, or a future
            # step Legistar lists with no meeting of ours behind it.
            e["upcoming"] = (meeting["kind"] == "Upcoming" if meeting
                             else e["date"] > today)
        # A joint hearing is one Legistar event per committee: show it once,
        # naming every committee, rather than as a row per committee.
        merged = []
        joint_row = {}
        for e in timeline:
            if not e["joint"]:
                merged.append(e)
                continue
            first = joint_row.get(e["date"])
            if first is None:
                e["bodies"] = [e["body"]]
                joint_row[e["date"]] = e
                merged.append(e)
            else:
                first["bodies"].append(e["body"])
                if action_rank(e) > action_rank(first):
                    first.update(action=e["action"], result=e["result"],
                                 tally=e["tally"] or first["tally"])
        for e in merged:
            if len(e.get("bodies", [])) > 1:
                e["body"] = "Joint hearing: " + ", ".join(
                    re.sub(r"^(Sub)?[Cc]ommittee on ", "", b) for b in e["bodies"])
        timeline = merged
        timeline.sort(key=lambda e: e["date"], reverse=True)
        m["appearances"] = timeline
        past = [e for e in timeline if not e["upcoming"]]
        future = [e for e in timeline if e["upcoming"]]
        # Nothing has happened to it yet: greyed like upcoming hearings.
        m["upcoming"] = bool(timeline) and not past
        m["last_action"] = past[0] if past else None
        m["latest_date"] = past[0]["date"] if past else ""
        m["next_date"] = future[-1]["date"] if future else ""
        m["companion_matters"] = [matter_by_slug[s] for s in m.get("companions", [])]
        m["committee"] = next((e.get("bodies", [e["body"]])[0] for e in timeline
                               if "committee" in e.get("bodies", [e["body"]])[0].lower()), "")
    matters.sort(key=lambda m: (m["latest_date"] or m["next_date"], m["file_number"]),
                 reverse=True)

    # --- finish members -------------------------------------------------
    for key, person in members.items():
        info = roster_by_name.get(key, {})
        person["district"] = info.get("district", "")
        person["party"] = info.get("party", "")
        person["borough"] = info.get("borough", "")
        person["area"] = info.get("area", "")
        for v in person["votes"]:
            v["link"] = v.pop("meeting")["link"]
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
    committee_slug_by_norm = {_norm_body(n): c["slug"] for n, c in committees.items()}
    for person in members.values():
        person["committee_links"] = [
            {"name": n, "slug": committee_slug_by_norm.get(_norm_body(n), "")}
            for n in person["committees"]]
    member_list = sorted(members.values(), key=lambda p: p["name"].split()[-1].lower())

    # --- finish committees ----------------------------------------------
    roster_chair = {normalize_name(n): e.get("chair", "")
                    for n, e in roster_committees.items()}
    committee_list = []
    for name, c in committees.items():
        c["chair"] = roster_chair.get(normalize_name(name), "")
        chair = members.get(normalize_name(c["chair"])) if c["chair"] else None
        c["chair_slug"] = chair["slug"] if chair else ""
        c["meetings"] = sorted((mt for mt in c["meetings"] if mt["linkable"]),
                               key=lambda mt: mt.get("date", ""), reverse=True)
        c["matters"] = sorted(
            (matter_by_slug[s] for s in c["matter_slugs"] if s in matter_by_slug),
            key=lambda m: m["file_number"],
        )
        del c["matter_slugs"]
        c["meeting_count"] = len(c["meetings"])
        committee_list.append(c)
    committee_list.sort(key=lambda c: c["name"])

    return {
        "meetings": {m["event_id"]: m for m in meetings},
        "meeting_list": meeting_list,
        "record_only_meetings": [m for m in meeting_list
                                 if not m["has_hearing"] and m["listed"]],
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
