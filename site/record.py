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
    # The Body column says which committee or subcommittee acted, so the
    # wording doesn't repeat it. A committee always hears an item on the day
    # it holds it over, so "held over" reads as "heard"; what happens next is
    # the matter's status note (stage_note), not part of the action.
    "Introduced by Council": "Introduced and sent to committee",
    "Referred to Comm by Council": "Sent to committee",
    "Re-referred to Committee by Council": "Sent back to committee",
    "Hearing Held by Committee": "Heard",
    "Hearing on P-C Item by Comm": "Heard",
    "Laid Over by Committee": "Heard",
    "Laid Over by Subcommittee": "Heard",
    "P-C Item Laid Over by Comm": "Heard",
    "Amendment Proposed by Comm": "Amended",
    "Amended by Committee": "Amended",
    "Approved by Committee": "Passed committee",
    "Approved by Committee with Companion Resolution": "Passed committee",
    "P-C Item Approved by Comm": "Passed committee",
    "P-C Item Approved by Committee with Companion Resolution": "Passed committee",
    "Approved by Subcommittee": "Passed committee",
    "Approved by Committee with Modifications and Referred to CPC":
        "Passed committee with changes",
    "Approved by Subcommittee with Modifications and Referred to CPC":
        "Passed committee with changes",
    "Disapproved by Committee": "Rejected",
    "Disapproved by Committee with Companion Resolution": "Rejected",
    "Disapproved by Subcommittee": "Rejected",
    "Disapproved by Council": "Rejected",
    "Filed, by Committee": "Closed",
    "Filed by Committee": "Closed",
    "Filed by Subcommittee": "Closed",
    "Filed by Council": "Closed",
    "Rcvd, Ord, Prnt, Fld by Council": "Closed",
    "Deferred": "Postponed",
    "Withdrawn": "Withdrawn",
    "Approved, by Council": "Passed Council",
    "Approved by Council": "Passed Council",
    "Approved with Modifications and Referred to the City Planning Commission "
    "pursuant to Section 197-(d) of the New York City Charter.":
        "Passed Council with changes",
    "Sent to Mayor by Council": "Sent to the Mayor",
    "Hearing Held by Mayor": "Mayor's hearing held",
    "Signed Into Law by Mayor": "Signed into law",
    "Recved from Mayor by Council": "Returned by the Mayor",
    "Returned Unsigned by Mayor": "Returned by the Mayor unsigned",
    "City Charter Rule Adopted": "Became law without the Mayor's signature",
}


def plain_action(action):
    if not action:
        return ""
    return PLAIN_ACTIONS.get(action.strip(), action)


# Where a matter stands, the same five stages for every type. "Passed
# Council" is the end of the line for everything but a bill, which goes on
# to the Mayor and becomes "Law". An oversight item is "In committee" until
# its hearing and "Closed" once the committee files it (the same day); the
# stage note then says "hearing held".
STAGES = ["In committee", "Passed committee", "Passed Council", "Law", "Closed"]

_STAGE_BY_STATUS = {
    "Introduced": "In committee",
    "Committee": "In committee",
    "Laid Over in Committee": "In committee",
    "Companion Pending Approval by Council": "Passed committee",
    "Enacted (Mayor's Desk for Signature)": "Passed Council",
    "Enacted": "Law",
    "Adopted": "Passed Council",
    "Filed": "Closed",
    "Withdrawn": "Closed",
    "Disapproved": "Closed",
    "Vetoed": "Closed",
    "Received, Ordered, Printed and Filed": "Closed",
}


# Resolutions that decide something (the budget, tax rates, appointments,
# funding, environmental findings) rather than express a view.
APPROVAL_RE = re.compile(
    r"^(a )?resolution (approving|pursuant|computing|by the new york city council"
    r"|to (provide|adopt|establish))\b", re.I)


def matter_type_label(matter):
    """Five plain types in place of Legistar's own."""
    t = matter.get("type") or ""
    if t == "Introduction":
        return "Proposed law"
    if t.startswith("Land Use"):
        return "Land use"
    if t == "Resolution":
        title = (matter.get("title") or matter.get("name") or "").strip()
        return "Approval" if APPROVAL_RE.match(title) else "Resolution"
    return t or "Matter"


def matter_stage(matter):
    status = matter.get("status") or ""
    stage = _STAGE_BY_STATUS.get(status, "")
    if matter.get("type") == "Oversight":
        return "Closed" if stage == "Closed" else "In committee"
    # Legistar leaves status at "Committee" between a committee vote and the
    # Council vote; the history tells them apart.
    history = matter.get("history") or []
    if stage == "In committee" and history:
        latest_day = [h for h in history if h.get("date") == history[0].get("date")]
        if any(plain_action(h.get("action")).startswith("Passed committee") for h in latest_day):
            stage = "Passed committee"
    return stage or status or "—"


def omb_note(fiscal):
    """Mention the Mayor's Office of Management and Budget only when it gave
    an estimate of its own (rare); its usual "did not provide" is dropped."""
    text = re.sub(r"\s+", " ", (fiscal or {}).get("omb_estimate") or "")
    if fiscal and "provided an estimate" in text and "not" not in text.split("provided")[0]:
        return "The Mayor's Office of Management and Budget also gave its own estimate, in the"
    return ""


# Every vote that isn't a yes, in the order a reader cares about.
VOTE_GROUPS = [("Negative", "Against"), ("Abstain", "Abstained"), ("Absent", "Absent"),
               ("Medical", "Medical leave"), ("Bereavement", "Bereavement leave"),
               ("Non-voting", "Non-voting")]


def vote_groups(roll_call):
    """[(label, [{name, slug}])] for one roll call: in favour, then the rest."""
    groups = []
    for code, label in [("Affirmative", "In favour")] + VOTE_GROUPS:
        names = [{"name": v["name"], "slug": slugify(normalize_name(v["name"]))}
                 for v in roll_call if v.get("vote") == code]
        if names:
            groups.append((label, names))
    return groups


VOTE_LABELS = dict([("Affirmative", "In favour")] + VOTE_GROUPS)


def vote_rows(roll_call):
    """Each member and how they voted, in Legistar's order."""
    return [{"name": v["name"], "slug": slugify(normalize_name(v["name"])),
             "code": (v.get("vote") or "").lower().replace(" ", "-"),
             "label": VOTE_LABELS.get(v.get("vote"), v.get("vote", ""))}
            for v in roll_call]


def vote_tip(roll_call):
    """The pop-up on a tally: who did not vote yes, and why."""
    lines = [f"{label}: " + ", ".join(n["name"] for n in names)
             for label, names in vote_groups(roll_call) if label != "In favour"]
    return "\n".join(lines) or "Every member voted in favour."


def lcfirst(text):
    """'Passed Council' -> 'passed Council', for use mid-sentence. Proper
    nouns at the start ("Mayor's hearing held") keep their capital."""
    if not text or text.split()[0].rstrip("'s") in ("Mayor", "Council", "City"):
        return text
    return text[0].lower() + text[1:]


def outcome(action, tally=None, upcoming=False):
    """What happened, in the words used everywhere: 'passed committee, 5–1'."""
    if not action:
        return "on the agenda" if upcoming else ""
    text = lcfirst(plain_action(action))
    if tally:
        text += f", {tally.get('Affirmative', 0)}–{tally.get('Negative', 0)}"
    return text


def stage_note(m):
    """What the stage means now, in a few words: what happened, or what
    the matter is waiting for. Shown in brackets after the stage."""
    stage, status, nxt = m["stage"], m.get("status") or "", m.get("next_date")
    oversight = m.get("type") == "Oversight"
    if stage == "Closed":
        if oversight:
            return "hearing held, no further action"
        return {"Withdrawn": "withdrawn", "Disapproved": "rejected",
                "Vetoed": "vetoed by the Mayor"}.get(status, "filed without a vote")
    # The next scheduled date is shown beside the last action, not here.
    if stage == "In committee":
        heard = any(plain_action(e["action"]) == "Heard"
                    for e in m["appearances"] if not e["upcoming"])
        return "heard, awaiting a vote" if heard and not oversight else "awaiting a hearing"
    if stage == "Passed committee":
        return "awaiting a Council vote"
    if stage == "Passed Council":
        return "awaiting the Mayor" if m.get("type") == "Introduction" else "adopted"
    if stage == "Law":
        return m.get("law_label", "")
    return ""


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


def _oversight_heard(entry):
    """An oversight topic is heard, then "filed" the same day. Nothing was
    closed and nothing is pending: the hearing happened, so say that."""
    if plain_action(entry.get("action")) in ("Closed", "Heard"):
        entry["action"] = "Hearing held"


def _preconsidered_aliases(matters):
    """Map each T-number that duplicates another matter to the one it duplicates.

    Legistar gives an item a T-number while it is preconsidered and a real
    number (Int, LU, Res, M) once introduced, and the two then live on as
    separate matters with the same title. A joint oversight hearing also gets
    one T-number per committee. Either way it is one item, so the T-number is
    folded into the real number, or into the lowest T-number of the set.
    """
    real_by_key = {}
    for m in matters:
        if not m["file_number"].startswith("T"):
            key = ((m.get("title") or "").strip(), m.get("type"))
            real_by_key.setdefault(key, []).append(m)
    alias = {}
    groups = {}
    for m in sorted(matters, key=lambda m: m["file_number"]):
        if not m["file_number"].startswith("T"):
            continue
        key = ((m.get("title") or "").strip(), m.get("type"))
        real = real_by_key.get(key, [])
        if len(real) == 1:
            alias[m["slug"]] = real[0]["slug"]
        elif not real and m.get("type") == "Oversight":
            # Same topic, same day: the per-committee copies of one hearing.
            first = min((h.get("date", "") for h in m.get("history") or []), default="")
            groups.setdefault(key + (first,), []).append(m)
    for group in groups.values():
        for m in group[1:]:
            alias[m["slug"]] = group[0]["slug"]
    return alias


def _merge_alias(canonical, dup):
    """Give the canonical matter the duplicate's history and documents."""
    seen = {(h.get("date"), h.get("body"), h.get("action")) for h in canonical.get("history") or []}
    canonical.setdefault("history", [])
    for h in dup.get("history") or []:
        if (h.get("date"), h.get("body"), h.get("action")) not in seen:
            canonical["history"].append(h)
    have = {a.get("label") for a in canonical.get("attachments") or []}
    canonical.setdefault("attachments", [])
    for a in dup.get("attachments") or []:
        if a.get("label") not in have:
            canonical["attachments"].append(a)
    if not canonical.get("sponsors"):
        canonical["sponsors"] = dup.get("sponsors") or []


DOC_DATE_RE = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})\b"
    r"|\b(January|February|March|April|May|June|July|August|September|October"
    r"|November|December) (\d{1,2}), (\d{4})\b")
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _doc_date(label):
    """The date in a document label ("Committee Report 3/3/26"), or ""."""
    m = DOC_DATE_RE.search(label)
    if not m:
        return ""
    if m.group(1):
        year = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return f"{m.group(6)}-{MONTHS.index(m.group(4)) + 1:02d}-{int(m.group(5)):02d}"


def _sort_documents(docs):
    """Undated documents (bill text, summaries, memos) first, in Legistar's
    order, then dated ones newest first, like the history."""
    undated = [d for d in docs if not _doc_date(d["label"])]
    dated = sorted((d for d in docs if _doc_date(d["label"])),
                   key=lambda d: _doc_date(d["label"]), reverse=True)
    return undated + dated


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
    for m in matters:
        if m.get("type") == "Oversight":
            for h in m.get("history") or []:
                _oversight_heard(h)
    alias = _preconsidered_aliases(matters)
    by_slug = {m["slug"]: m for m in matters}
    for dup, canon in alias.items():
        _merge_alias(by_slug[canon], by_slug[dup])
    aliased = [by_slug[s] for s in alias]
    matters = [m for m in matters if m["slug"] not in alias]
    for meeting in meetings:
        meeting["items"] = [i for i in meeting.get("items", [])
                            if i.get("type") not in EXCLUDED_TYPES]
        # Agendas name the T-number; show the item under its real number.
        for i in meeting["items"]:
            if i.get("type") == "Oversight":
                _oversight_heard(i)
            canon = alias.get(slugify(i.get("file_number") or ""))
            if canon:
                i["file_number"] = by_slug[canon]["file_number"]
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
        if not p:
            p = next((plain[d["file_number"]] for d in aliased
                      if alias[d["slug"]] == m["slug"] and d["file_number"] in plain), {})
        m["plain_title"] = p.get("title", "")
        m["plain_summary"] = p.get("summary", "")
        m["display_status"] = m.get("status") or "—"
        m["stage"] = matter_stage(m)
        # A bill that has become law is no longer a proposed law.
        m["type_label"] = "Law" if m["stage"] == "Law" else matter_type_label(m)
        m["short_title"] = m["plain_title"] or (m.get("name") or m.get("title") or "").strip()
    _link_companions(matters, matter_by_slug)
    # A land use application and the resolution that decides it are one item.
    # The application carries the whole history (referral, hearings, votes);
    # the resolution only appears at the end and repeats the last two steps,
    # so it is folded into the application rather than listed on its own.
    folded = {m["slug"]: m["companions"][0] for m in matters
              if m["file_number"].startswith("Res") and m["companions"]}
    for slug, lu_slug in folded.items():
        matter_by_slug[slug]["folded_into"] = lu_slug
    # T-number duplicates redirect and link to their real matter too.
    for d in aliased:
        d["folded_into"] = alias[d["slug"]]
        matter_by_slug[d["slug"]] = d
        folded[d["slug"]] = alias[d["slug"]]

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

        lu_voted = {i.get("file_number") for i in meeting.get("items", [])
                    if i.get("roll_call")}
        for item in meeting.get("items", []):
            slug = item.get("file_number") and slugify(item["file_number"])
            matter = matter_by_slug.get(slug)
            file_number = item.get("file_number", "")
            if slug in folded:
                # Count the vote once, against the application.
                matter = matter_by_slug[folded[slug]]
                if matter["file_number"] in lu_voted:
                    continue
                slug, file_number = matter["slug"], matter["file_number"]
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
                    "file_number": file_number,
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
        # Legistar files the Mayor's steps (signing, or a bill becoming law
        # unsigned) under "Administration".
        entries = [{"date": h.get("date", ""),
                    "body": "Mayor's office" if h.get("body") == "Administration" else h.get("body", ""),
                    "action": h.get("action", ""), "result": h.get("result", "")}
                   for h in m.get("history") or []]
        if any(e["action"] == "City Charter Rule Adopted" for e in entries):
            entries = [e for e in entries if e["action"] != "Returned Unsigned by Mayor"]
        timeline = collapse(entries, key=lambda e: (e["date"], _norm_body(e["body"])))
        by_key = {(e["date"], _norm_body(e["body"])): e for e in timeline}
        # A meeting in data/ that the history does not mention still counts,
        # and a meeting that records a more decisive step wins: Legistar
        # updates a matter's history a day or so after the meeting does.
        for meeting, row in rows_by_matter.get(m["slug"], []):
            k = (meeting.get("date", ""), _norm_body(meeting.get("body", "")))
            e = by_key.get(k)
            if e is None:
                by_key[k] = {"date": k[0], "body": meeting.get("body", ""),
                             "action": row.get("action", ""),
                             "result": row.get("result", "")}
                timeline.append(by_key[k])
            elif action_rank(row) > action_rank(e):
                e.update(action=row.get("action", ""), result=row.get("result", ""))
            # ...and the meeting's record shows the same step as the history
            # ("Introduced", where the Stated Meeting agenda says "Referred").
            e = by_key[k]
            row["action"], row["result"] = e["action"], e["result"]
        slugs = {m["slug"], *m.get("companions", [])}
        for e in timeline:
            meeting = meeting_by_day_body.get((e["date"], _norm_body(e["body"])))
            e["date_display"] = display_date(e["date"])
            e["link"] = meeting["link"] if meeting and meeting["linkable"] else ""
            # A joint hearing's sibling committees often have no event in
            # data/; the published hearing still covers them.
            hearing = None if e["link"] else hearing_by_day_body.get((e["date"], _norm_body(e["body"])))
            if hearing:
                e["link"] = f"/hearings/{hearing['slug']}/"
            e["tally"] = {}
            if meeting:
                for row in meeting["rows"]:
                    if row["matter_slug"] in slugs and row.get("tally"):
                        e["tally"] = row["tally"]
                        break
            e["joint"] = bool(meeting and "jointly" in (meeting.get("location") or "").lower()
                              or hearing and len(hearing.get("committee_list", [])) > 1)
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
        for e in timeline:
            e["outcome"] = "scheduled" if e["upcoming"] else outcome(e["action"], e["tally"])
        timeline.sort(key=lambda e: e["date"], reverse=True)
        m["appearances"] = timeline
        # Every roll call on this matter, newest first: the one place the site
        # names how each member voted.
        m["votes"] = sorted(
            ({"date": meeting.get("date", ""), "body": meeting.get("body", ""),
              "link": meeting["link"] if meeting["linkable"] else "",
              "outcome": outcome(rc.get("action") or row.get("action"), rc.get("tally")),
              "members": vote_rows(rc["roll_call"])}
             for meeting, row in rows_by_matter.get(m["slug"], [])
             for rc in row.get("roll_calls") or []),
            # Same day: the Council votes after the committee, as in History.
            key=lambda v: (v["date"], _norm_body(v["body"]) == _norm_body("City Council")),
            reverse=True)
        # Each roll call hangs off its history line, opened from the bracket.
        for e in timeline:
            bodies = {_norm_body(b) for b in e.get("bodies", [e["body"]])}
            vote = next((v for v in m["votes"]
                         if v["date"] == e["date"] and _norm_body(v["body"]) in bodies), None)
            e["vote_members"] = vote["members"] if vote else []
        past = [e for e in timeline if not e["upcoming"]]
        future = [e for e in timeline if e["upcoming"]]
        # Nothing has happened to it yet: greyed like upcoming hearings.
        m["upcoming"] = bool(timeline) and not past
        m["last_action"] = past[0] if past else None
        m["latest_date"] = past[0]["date"] if past else ""
        m["next_date"] = future[-1]["date"] if future else ""
        m["companion_matters"] = [matter_by_slug[s] for s in m.get("companions", [])]
        # "2026/135" is how Legistar writes Local Law 135 of 2026.
        year, _, num = (m.get("law_number") or "").partition("/")
        m["law_label"] = f"Local Law {int(num)} of {year}" if num.isdigit() else ""
        m["stage_note"] = stage_note(m)
        m["omb_note"] = omb_note(m.get("fiscal"))
        # The deciding resolution's documents join the application's, less
        # the ones Legistar already files under both.
        docs = list(m.get("attachments") or [])
        labels = {d["label"] for d in docs}
        for c in m["companion_matters"]:
            if c.get("folded_into"):
                docs += [d for d in c.get("attachments") or [] if d["label"] not in labels]
        m["documents"] = _sort_documents(docs)
        m["committee"] = next((e.get("bodies", [e["body"]])[0] for e in timeline
                               if "committee" in e.get("bodies", [e["body"]])[0].lower()), "")
    matters.sort(key=lambda m: (m["latest_date"] or m["next_date"], m["file_number"]),
                 reverse=True)

    # Each agenda line says, in one bracket, what happened to its matter at
    # the meeting and, where it adds something, where the matter stands now,
    # in the same words as the stage notes: "heard; awaiting a vote".
    for meeting in meetings:
        upcoming = meeting["kind"] == "Upcoming"
        for row in meeting["rows"]:
            m = matter_by_slug.get(row["matter_slug"])
            done = outcome(row.get("action"), row.get("tally"), upcoming)
            row["outcome"] = done
            row["type_label"] = m["type_label"] if m is not None else "Other"
            tally = row.get("tally")
            row["tally_text"] = f"{tally.get('Affirmative', 0)}–{tally.get('Negative', 0)}" if tally else ""
            calls = [c for c in row.get("roll_calls") or [] if c.get("tally") == tally] or row.get("roll_calls") or []
            row["vote_tip"] = vote_tip(calls[-1]["roll_call"]) if calls else ""
            if m is None or "stage_note" not in m or m.get("type") == "Oversight" or upcoming:
                continue
            last = m.get("last_action") or {}
            moved_on = (last.get("date", ""), _norm_body(last.get("body", ""))) != (
                meeting.get("date", ""), _norm_body(meeting.get("body", "")))
            note = m["stage_note"].removeprefix("heard, ")
            stage = m["stage"]
            now = ""
            if moved_on and stage == "Law":
                now = f"now {note}" if note else "now law"
            elif moved_on and stage in ("Passed Council", "Passed committee", "Closed"):
                reached = note if stage == "Closed" else lcfirst(stage)
                now = f"since {reached}" + ("" if stage == "Closed" or note == "adopted" else f", {note}")
            elif stage not in ("Closed", "Law") and note != "adopted":
                now = note
            if now:
                row["outcome"] = f"{done}; {now}" if done else now

    # --- finish members -------------------------------------------------
    for key, person in members.items():
        info = roster_by_name.get(key, {})
        person["district"] = info.get("district", "")
        person["roster_name"] = info.get("roster_name", "")
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
    chair_of = {}
    for name, entry in roster_committees.items():
        if entry.get("chair"):
            chair_of.setdefault(normalize_name(entry["chair"]), []).append(
                re.sub(r"^(Sub)?[Cc]ommittee on ", "", name))
    for key, person in members.items():
        person["chairs"] = sorted(chair_of.get(key, []))
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
        "matters": [m for m in matters if m["slug"] not in folded],
        "matter_by_slug": matter_by_slug,
        "folded": folded,
        "members": member_list,
        "committees": committee_list,
    }


# --- linking member names in prose -------------------------------------

MEMBER_TITLE_RE = (r"(?:CM|Chair|Co-Chair|Speaker|Deputy Speaker|Majority Leader|"
                   r"Minority Leader|Council ?Members?|Councilmembers?)")
_PARTICLES = {"De", "Del", "La", "Van", "Von", "Da"}


def _fold(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _surname(name):
    tokens = [t for t in name.split()[1:] if not re.fullmatch(r"[A-Z]\.", t)]
    for i, t in enumerate(tokens):
        if t in _PARTICLES:
            return " ".join(tokens[i:])
    return tokens[-1] if tokens else ""


def member_name_linker(members):
    """Build a function that links current members' names in rendered prose.

    Summary names come from captions, so only exact matches are linked: a
    current member's full name, or a title ("CM", "Chair", "Speaker",
    "Council Members A, B and C") followed by a surname that belongs to
    exactly one current member. A garble that happens to spell another real
    member's name will still link to that member; that risk is accepted.
    """
    current = [p for p in members if p.get("district")]
    by_form = {}
    surnames = {}
    for p in current:
        for full in {p["name"], p.get("roster_name") or p["name"]}:
            for form in {full, _fold(full), re.sub(r" [A-Z]\. ", " ", full)}:
                by_form[form] = p["slug"]
        sur = _surname(p.get("roster_name") or p["name"])
        surnames.setdefault(sur, set()).add(p["slug"])
    unique = {}
    for sur, slugs in surnames.items():
        if len(slugs) == 1:
            unique[sur] = unique[_fold(sur)] = next(iter(slugs))
    if not by_form:
        return lambda html: html
    alt = lambda forms: "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    full_re = re.compile(rf"\b({alt(by_form)})\b")
    sur = alt(unique)
    titled_re = re.compile(
        rf"\b({MEMBER_TITLE_RE}) ((?:{sur})(?:(?:, | and |, and )(?:{sur}))*)\b")
    one_re = re.compile(rf"\b({sur})\b")

    def link(slug, text):
        return f'<a class="member-ref" href="/members/{slug}/">{text}</a>'

    def text_node(t):
        out, pos = [], 0
        spans = []
        for m in full_re.finditer(t):
            spans.append((m.start(), m.end(), link(by_form[m.group(1)], m.group(1))))
        for m in titled_re.finditer(t):
            names_start = m.start(2)
            for n in one_re.finditer(m.group(2)):
                a, b = names_start + n.start(), names_start + n.end()
                if not any(x <= a < y for x, y, _ in spans):
                    spans.append((a, b, link(unique[n.group(1)], n.group(1))))
        for a, b, rep in sorted(spans):
            if a < pos:
                continue
            out.append(t[pos:a])
            out.append(rep)
            pos = b
        out.append(t[pos:])
        return "".join(out)

    def apply(html):
        if not html:
            return html
        parts = re.split(r"(<[^>]+>)", html)
        depth = 0
        for i, part in enumerate(parts):
            if part.startswith("<"):
                if re.match(r"<a\b", part):
                    depth += 1
                elif part.startswith("</a"):
                    depth -= 1
            elif depth == 0 and part.strip():
                parts[i] = text_node(part)
        return "".join(parts)

    return apply


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
        slug = matter_by_slug[slug].get("folded_into") or slug
        return f'<a class="matter-ref" href="/matters/{slug}/">{label}</a>'

    # Don't rewrite inside an existing anchor or tag attribute.
    parts = re.split(r"(<[^>]+>)", html)
    for i, part in enumerate(parts):
        if not part.startswith("<"):
            parts[i] = MATTER_RE.sub(repl, part)
    return "".join(parts)
