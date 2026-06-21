"""
Assemble a ready-to-send weekly digest email for Hearing Hearings.

Collects every hearing published within a date window (default: the trailing
7 days), pulls the first 2-3 sentences of each hearing's existing Meeting
Overview verbatim as a brief, and renders a self-contained, Win95-styled HTML
file matching the site / daily-email design. The file is meant to be pasted
into MailerLite's Custom HTML editor, where the user writes the opening
commentary and sends manually.

No API key required. Reuses the pure helpers in site/build.py.

Usage:
    python weekly_digest.py
    python weekly_digest.py --since 2026-06-15 --until 2026-06-21
    python weekly_digest.py --sentences 3 --output my-digest.html
"""

import argparse
import html as html_mod
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# site/ holds build.py. We import it by adding that dir to sys.path and doing a
# bare `import build` -- NOT `from site import build`, because `site` collides
# with Python's stdlib site module. Only side-effect-free helpers are used.
REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "site"))
import build  # noqa: E402  (parse_front_matter, split_summary_transcript, truncate_text, SITE_URL, CONTENT_DIR)

OUTPUT_DIR = REPO_ROOT / "weekly_digests"

# --- Palette (mirrors _render_email_html in summarize_council_meeting.py, which
#     itself hardcodes the site CSS variables for email-client compatibility) ---
COL_CANVAS = "#5e5e60"        # outer gray
COL_PAPER = "#fbfaf6"         # window body
COL_BEVEL_LIGHT = "#ffffff"   # top/left frame
COL_BEVEL_DARK = "#4a4740"    # bottom/right frame
COL_TITLEBAR = "#1d1d1f"
COL_ACCENT = "#f5c451"        # titlebar glyph yellow
COL_STATUSBAR = "#c8c5bc"
COL_TEXT = "#15171a"
COL_MUTED = "#5a574f"
COL_LINK = "#1c4f8c"
COL_DIVIDER = "#d6d3c8"

# Abbreviations whose trailing period must not be read as a sentence end.
ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "st", "sr", "jr", "vs", "inc", "co", "corp",
    "ltd", "etc", "no", "gov", "sen", "rep", "rev", "col", "gen", "lt",
    "sgt", "capt", "hon", "asst", "dept", "fig", "approx",
}

# Runaway backstop only. Briefs are whole sentences (these Meeting Overviews
# run ~300 chars/sentence, so two complete sentences land near 600-700); this
# cap just prevents a pathologically long single sentence dumping a paragraph.
BRIEF_MAX_CHARS = 800


def parse_args():
    today = datetime.now().date()
    parser = argparse.ArgumentParser(
        description="Assemble a ready-to-send weekly digest email."
    )
    parser.add_argument(
        "--since", type=_parse_date, default=today - timedelta(days=7),
        help="Start of the window (YYYY-MM-DD, inclusive). Default: 7 days ago.",
    )
    parser.add_argument(
        "--until", type=_parse_date, default=today,
        help="End of the window (YYYY-MM-DD, inclusive). Default: today.",
    )
    parser.add_argument(
        "--sentences", type=int, default=2,
        help="Sentences to take from each Meeting Overview (1-3). Default: 2.",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output HTML path. Default: weekly_digests/weekly-digest-<until>.html",
    )
    return parser.parse_args()


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def collect_hearings(since, until):
    """Return hearing dicts whose date falls in [since, until], newest first."""
    hearings = []
    for filename in sorted(os.listdir(build.CONTENT_DIR)):
        if not filename.endswith(".md"):
            continue
        with open(os.path.join(build.CONTENT_DIR, filename), encoding="utf-8") as f:
            text = f.read()
        meta, body = build.parse_front_matter(text)
        date_str = meta.get("date", "")
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (since <= date_obj <= until):
            continue
        summary_md, _ = build.split_summary_transcript(body)
        hearings.append({
            "committee": meta.get("committee", ""),
            "title": meta.get("title", filename),
            "slug": meta.get("slug", filename[:-3]),
            "date": date_obj,
            "duration": meta.get("duration", ""),
            "youtube_url": meta.get("youtube_url", ""),
            "viebit_url": meta.get("viebit_url", ""),
            "council_url": meta.get("council_url", ""),
            "brief": "",  # filled below
            "_summary_md": summary_md,
        })
    hearings.sort(key=lambda h: h["date"], reverse=True)
    return hearings


def extract_meeting_overview(summary_md):
    """Return the Meeting Overview body text (bare-label section in the md)."""
    m = re.search(
        r"(?ms)^\s*Meeting Overview\s*$\n(.*?)\n\s*Numbers\s*$", summary_md
    )
    if m:
        return m.group(1).strip()
    # Fallback: everything after the Meeting Overview label.
    m = re.search(r"(?ms)^\s*Meeting Overview\s*$\n(.*)", summary_md)
    if m:
        return m.group(1).strip()
    return summary_md.strip()


def first_sentences(text, n=2, max_chars=BRIEF_MAX_CHARS):
    """Take the first n sentences (1-3) of text verbatim, ending cleanly.

    Guards against false sentence breaks on ellipses (...) and known
    abbreviations, and caps the result so a single long sentence can't run
    away.
    """
    n = max(1, min(n, 3))
    text = " ".join(text.split())  # normalize whitespace
    if not text:
        return ""

    # Mask ellipses and abbreviation periods so they don't trigger splits.
    ELLIPSIS = "\x00"
    ABBR_DOT = "\x01"
    masked = text.replace("...", ELLIPSIS).replace("…", ELLIPSIS)
    abbr_re = re.compile(
        r"\b(" + "|".join(sorted(ABBREVIATIONS, key=len, reverse=True)) + r")\.",
        re.IGNORECASE,
    )
    masked = abbr_re.sub(lambda m: m.group(0).replace(".", ABBR_DOT), masked)

    # Split on sentence-ending punctuation followed by a space + opener.
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'(“‘]?[A-Z0-9])", masked)

    def restore(s):
        return s.replace(ELLIPSIS, "...").replace(ABBR_DOT, ".").strip()

    sentences = [restore(p) for p in parts if restore(p)]

    # Take the first n complete sentences. Only fall back to trimming if their
    # combined length blows past the runaway backstop, and even then keep whole
    # sentences (always at least the first) so the brief ends cleanly.
    chosen = sentences[:n]
    brief = " ".join(chosen).strip()
    if len(brief) > max_chars and chosen:
        kept = [chosen[0]]
        for s in chosen[1:]:
            if len(" ".join(kept + [s])) > max_chars:
                break
            kept.append(s)
        brief = " ".join(kept).strip()
        if len(brief) > max_chars:  # a single sentence longer than the cap
            brief = build.truncate_text(brief, max_chars)
    return brief


def format_week_range(since, until):
    """Human-friendly window label, e.g. 'June 16-22, 2026'."""
    if since == until:
        return since.strftime("%B %#d, %Y")
    if since.year == until.year and since.month == until.month:
        return f"{since.strftime('%B %#d')}-{until.strftime('%#d')}, {until.year}"
    if since.year == until.year:
        return f"{since.strftime('%B %#d')} - {until.strftime('%B %#d')}, {until.year}"
    return f"{since.strftime('%B %#d, %Y')} - {until.strftime('%B %#d, %Y')}"


def _watch_link(h):
    url = h["youtube_url"] or h["viebit_url"]
    if not url:
        return ""
    return (
        f'<a href="{html_mod.escape(url, quote=True)}" '
        f'style="color: {COL_LINK}; text-decoration: none;">Watch &#8599;</a>'
    )


def render_card(h):
    hearing_url = f"{build.SITE_URL}/hearings/{h['slug']}/"
    committee = html_mod.escape(h["committee"])
    title = html_mod.escape(h["title"])
    date_display = h["date"].strftime("%B %#d, %Y")

    meta_parts = [date_display]
    if h["duration"]:
        meta_parts.append(html_mod.escape(h["duration"]))
    watch = _watch_link(h)
    if watch:
        meta_parts.append(watch)
    meta_html = " &middot; ".join(meta_parts)

    committee_html = (
        f'<p style="font-family: \'IBM Plex Mono\', Consolas, monospace; '
        f'font-size: 12px; font-weight: 600; letter-spacing: 0.04em; '
        f'color: {COL_MUTED}; margin: 0 0 6px 0;">{committee}</p>'
        if committee else ""
    )

    return f"""\
<tr><td style="padding: 24px 24px 22px 24px; border-top: 1px dashed {COL_DIVIDER}; background-color: {COL_PAPER};" bgcolor="{COL_PAPER}">
  {committee_html}
  <h2 style="font-family: Inter, Arial, sans-serif; font-size: 19px; font-weight: 700;
             line-height: 1.25; letter-spacing: -0.01em; margin: 0 0 8px 0;">
    <a href="{hearing_url}" style="color: {COL_TEXT}; text-decoration: none;">{title}</a>
  </h2>
  <div style="font-family: 'IBM Plex Mono', Consolas, monospace; font-size: 12px;
              text-transform: uppercase; letter-spacing: 0.08em; color: {COL_MUTED}; margin-bottom: 12px;">
    {meta_html}
  </div>
  <p style="font-family: Inter, Arial, sans-serif; font-size: 16px; line-height: 1.6;
            color: {COL_TEXT}; margin: 0 0 12px 0;">{html_mod.escape(h['brief'])}</p>
  <p style="margin: 0;">
    <a href="{hearing_url}" style="font-family: 'IBM Plex Mono', Consolas, monospace;
       font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
       color: {COL_LINK}; text-decoration: none;">Read more &#8599;</a>
  </p>
</td></tr>"""


def render_html(hearings, since, until):
    week_label = format_week_range(since, until)
    cards = "\n".join(render_card(h) for h in hearings)

    return f"""\
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; background-color: {COL_PAPER}; -webkit-text-size-adjust: 100%;" bgcolor="{COL_PAPER}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background-color: {COL_CANVAS};" bgcolor="{COL_CANVAS}">
<tr><td align="center" style="padding: 24px 16px; background-color: {COL_CANVAS};" bgcolor="{COL_CANVAS}">

<!-- Window frame -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="max-width: 700px; background: {COL_PAPER};
              border-top: 3px solid {COL_BEVEL_LIGHT}; border-left: 3px solid {COL_BEVEL_LIGHT};
              border-right: 3px solid {COL_BEVEL_DARK}; border-bottom: 3px solid {COL_BEVEL_DARK};"
       bgcolor="{COL_PAPER}">

<!-- Title bar -->
<tr><td style="background: {COL_TITLEBAR}; padding: 8px 12px;
               font-family: 'IBM Plex Mono', Consolas, monospace;
               font-size: 12px; font-weight: 600; letter-spacing: 0.04em; color: #ffffff;"
        bgcolor="{COL_TITLEBAR}">
  <a href="{build.SITE_URL}" style="color: #ffffff; text-decoration: none;">
    <span style="color: {COL_ACCENT};">&#9619;</span> Hearing Hearings</a>
  &mdash; Week of {week_label}
</td></tr>

<!-- Window body -->
<tr><td style="padding: 0;" bgcolor="{COL_PAPER}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background-color: {COL_PAPER};" bgcolor="{COL_PAPER}">

<!-- Skyline -->
<tr><td style="padding: 22px 24px 4px 24px; background-color: {COL_PAPER};" bgcolor="{COL_PAPER}">
  <img src="{build.SITE_URL}/static/skyline-email.png" alt="NYC Skyline" width="320"
       style="display: block; width: 80%; max-width: 320px; height: auto; opacity: 0.32;">
</td></tr>

<!-- ===== COMMENTARY: replace the placeholder paragraph below with the week's intro ===== -->
<tr><td style="padding: 8px 24px 24px 24px; background-color: {COL_PAPER};" bgcolor="{COL_PAPER}">
  <p style="font-family: Inter, Arial, sans-serif; font-size: 17px; line-height: 1.65;
            color: {COL_TEXT}; margin: 0;">[ Write the week's commentary here. ]</p>
</td></tr>
<!-- ===== END COMMENTARY ===== -->

<!-- Section heading -->
<tr><td style="padding: 0 24px; background-color: {COL_PAPER};" bgcolor="{COL_PAPER}">
  <div style="font-family: 'IBM Plex Mono', Consolas, monospace; font-size: 15px; font-weight: 600;
              text-transform: uppercase; letter-spacing: 0.09em; color: {COL_MUTED};
              padding-bottom: 6px; border-bottom: 1px dashed {COL_DIVIDER};">This Week's Hearings</div>
</td></tr>

{cards}

</table>
</td></tr>

<!-- Status bar -->
<tr><td style="padding: 6px 12px; font-family: 'IBM Plex Mono', Consolas, monospace;
               font-size: 11px; color: {COL_MUTED}; border-top: 1px solid {COL_BEVEL_DARK};
               background: {COL_STATUSBAR};" bgcolor="{COL_STATUSBAR}">
  <a href="{build.SITE_URL}" style="color: {COL_MUTED}; text-decoration: none;">hearinghearings.nyc</a>
</td></tr>

</table>
<!-- End window frame -->

</td></tr>
</table>
</body>
</html>"""


def main():
    args = parse_args()
    since, until = args.since, args.until
    if since > until:
        print(f"--since ({since}) is after --until ({until}).", file=sys.stderr)
        sys.exit(1)

    hearings = collect_hearings(since, until)
    if not hearings:
        print(
            f"No hearings dated {since} to {until} (inclusive).\n"
            f"Adjust the window with --since / --until. Nothing written."
        )
        sys.exit(0)

    for h in hearings:
        overview = extract_meeting_overview(h["_summary_md"])
        h["brief"] = first_sentences(overview, n=args.sentences)

    html_out = render_html(hearings, since, until)

    if args.output:
        out_path = Path(args.output)
    else:
        OUTPUT_DIR.mkdir(exist_ok=True)
        out_path = OUTPUT_DIR / f"weekly-digest-{until}.html"
    out_path.write_text(html_out, encoding="utf-8")

    week_label = format_week_range(since, until)
    print(f"\nWeekly digest -- week of {week_label}")
    print(f"  Window: {since} to {until} (inclusive)")
    print(f"  {len(hearings)} hearing(s):")
    for h in hearings:
        print(f"    {h['date']}  {h['committee']} -- {h['title']}")
    print(f"\n  Written to: {out_path}")
    print(f"  Suggested subject: Hearing Hearings -- week of {week_label}")
    print(
        "\n  Next: open the file, or paste its HTML into MailerLite "
        "(Create Campaign -> Custom HTML editor, available on free from "
        "July 1 2026), write the commentary in the marked block, set the "
        "subject, send a preview to yourself, then broadcast."
    )


if __name__ == "__main__":
    main()
