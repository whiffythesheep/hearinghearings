"""Build static HTML site from markdown content files."""

import html as html_mod
import json
import os
import re
import shutil
import markdown

from collections import Counter
from datetime import datetime
from jinja2 import Environment, FileSystemLoader

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR = os.path.join(os.path.dirname(ROOT), "content")
TEMPLATE_DIR = os.path.join(ROOT, "templates")
STATIC_DIR = os.path.join(ROOT, "static")
OUTPUT_DIR = os.path.join(ROOT, "output")
SITE_URL = "https://hearinghearings.nyc"
META_IMAGE = f"{SITE_URL}/static/og-image.png"

# Tokenizer contract shared with the search JS in index.html — must match exactly.
# Decimal/comma/dollar/percent aware: "." or "," continues a token only when a
# digit follows ("12.7" and "12,000" are single tokens; "12." and "e.g." split,
# and "2026, 300" splits at the comma-space); "$" attaches only when a digit
# follows ("$3.5" is one token, a lone "$" is a separator); a trailing "%"
# attaches to digit-led tokens ("50%" is one token).
SEARCH_TOKEN_RE = re.compile(r"\$?\d[a-z0-9']*(?:[.,]\d[a-z0-9']*)*%?|[a-z0-9']+")

# Candidate rotating search-bar examples. Only phrases that actually occur in a
# published transcript are shipped, so an example never returns zero results.
SEARCH_EXAMPLE_CANDIDATES = [
    "congestion pricing",
    "e-bikes",
    "CityFHEPS",
    "outdoor dining",
    "trash containerization",
    "artificial intelligence",
    "asylum seekers",
    "property tax",
    "school buses",
    "street vendors",
    "child care",
    "composting",
    "rats",
    "affordable housing",
]


def truncate_text(text, max_len):
    """Truncate text to max_len chars at a word boundary."""
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def parse_front_matter(text):
    """Extract YAML front matter and body from markdown text."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text

    meta = {}
    for line in match.group(1).strip().split("\n"):
        key, _, value = line.partition(":")
        value = value.strip().strip('"')
        meta[key.strip()] = value

    return meta, match.group(2).strip()


SECTION_LABELS_STRIP = {"Summary"}
SECTION_LABELS_H3 = {"Meeting Overview", "Numbers", "Action Points"}


def promote_section_headings(md_text):
    """Convert bare section-label lines (e.g. 'Numbers') to markdown headings.

    The summarizer emits these as plain paragraphs. Promote subsection labels
    to h3 so templates can style them. The literal 'Summary' line is dropped —
    the template renders the Summary heading itself (so it can pair with a
    download button alongside).
    """
    out_lines = []
    for line in md_text.split("\n"):
        stripped = line.strip()
        if stripped in SECTION_LABELS_STRIP:
            continue
        if stripped in SECTION_LABELS_H3:
            out_lines.append(f"### {stripped}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def markdown_to_text(md):
    """Strip markdown formatting for plain-text download output."""
    # Links: [text](url) → text url
    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", md)
    # Bold/italic
    md = re.sub(r"\*\*([^*]+)\*\*", r"\1", md)
    md = re.sub(r"\*([^*]+)\*", r"\1", md)
    md = re.sub(r"__([^_]+)__", r"\1", md)
    md = re.sub(r"_([^_]+)_", r"\1", md)
    # Inline code
    md = re.sub(r"`([^`]+)`", r"\1", md)
    # Headings: drop the leading hashes
    md = re.sub(r"^#{1,6}\s+(.+)$", r"\1", md, flags=re.MULTILINE)
    # Collapse runs of 3+ blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def split_summary_transcript(body):
    """Split markdown body into summary and transcript sections."""
    # Look for ## Full Transcript heading
    match = re.search(r"^## Full Transcript\s*$", body, re.MULTILINE)
    if match:
        summary = body[: match.start()].strip()
        transcript = body[match.end() :].strip()
        return summary, transcript
    return body, ""


def search_tokenize(text):
    return SEARCH_TOKEN_RE.findall(text.lower())


def search_phrase_pattern(phrase):
    """Regex matching the phrase's tokens separated by up to 3 non-token chars.

    Tokens are re.escape()d, so "." and "$" in decimal/dollar tokens are
    literal. Digit-edged tokens get extra guards so "12" cannot anchor
    inside the decimal token "312" or "12.7".
    """
    tokens = search_tokenize(phrase)
    core = r"[^a-z0-9']{1,3}".join(re.escape(t) for t in tokens)
    pre = r"(?<![a-z0-9'])"
    if tokens and tokens[0][0] in "$0123456789":
        pre += r"(?<!\d[.,])"
    post = r"(?![a-z0-9'])"
    if tokens and tokens[-1][-1].isdigit():
        post = r"(?![a-z0-9']|[.,]\d)"
    return re.compile(pre + core + post)


def search_token_variants(token):
    """Closure of alternate spellings a numeric token is dual-indexed under.

    "$12,000" -> {"$12,000", "12,000", "$12000", "12000"}; "50%" -> {"50%",
    "50"}; "12000" -> {"12000", "12,000"}; "0943" -> {"0943", "943"}. Bare
    number queries then surface every spelling, while "$"/"%"-qualified
    queries match only the qualified occurrences. Non-numeric tokens return
    just themselves.
    """
    seen = set()
    stack = [token]
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        if t.startswith("$"):
            stack.append(t[1:])
        if t.endswith("%"):
            stack.append(t[:-1])
        if "," in t:
            stack.append(t.replace(",", ""))
        else:
            m = re.fullmatch(r"(\$?)([1-9]\d{3,})((?:\.\d+)?%?)", t)
            if m:
                sign, digits, rest = m.groups()
                stack.append(f"{sign}{int(digits):,}{rest}")
        if re.fullmatch(r"0\d+", t):
            stack.append(t.lstrip("0") or "0")
    return seen


def select_search_examples(hearings):
    texts = [h["transcript_text"].lower() for h in hearings if h["transcript_text"]]
    examples = []
    for candidate in SEARCH_EXAMPLE_CANDIDATES:
        pattern = search_phrase_pattern(candidate)
        if any(pattern.search(t) for t in texts):
            examples.append(candidate)
    return examples


def build_search_index(hearings):
    """Write the inverted index consumed by the transcript search JS.

    docs.json is sorted date ascending (tiebreak: slug) so a new hearing
    appends at the end and existing doc indices never shift between builds.
    """
    docs = sorted(
        (h for h in hearings if h["transcript_text"]),
        key=lambda h: (h["date"], h["slug"]),
    )
    search_dir = os.path.join(OUTPUT_DIR, "search")
    os.makedirs(search_dir)
    with open(os.path.join(search_dir, "docs.json"), "w", encoding="utf-8") as f:
        json.dump([h["slug"] for h in docs], f, separators=(",", ":"))

    shards = {}
    for i, hearing in enumerate(docs):
        raw_counts = Counter(search_tokenize(hearing["transcript_text"]))
        # Dual indexing: every alternate spelling of a numeric token also
        # counts toward its twins (see search_token_variants), so a query in
        # any spelling surfaces every spelling.
        counts = Counter()
        for token, count in raw_counts.items():
            for variant in search_token_variants(token):
                counts[variant] += count
        for token, count in counts.items():
            first = token[0]
            shard_key = first if first.isalpha() else "0"
            shards.setdefault(shard_key, {}).setdefault(token, []).append([i, count])

    for shard_key in sorted(shards):
        tokens = shards[shard_key]
        with open(os.path.join(search_dir, f"idx-{shard_key}.json"), "w", encoding="utf-8") as f:
            json.dump({t: tokens[t] for t in sorted(tokens)}, f, separators=(",", ":"))
    print(f"Built: search/ ({len(docs)} docs, {len(shards)} shards)")


def load_content():
    """Load all markdown content files."""
    hearings = []
    for filename in sorted(os.listdir(CONTENT_DIR)):
        if not filename.endswith(".md"):
            continue

        with open(os.path.join(CONTENT_DIR, filename), encoding="utf-8") as f:
            text = f.read()

        meta, body = parse_front_matter(text)
        summary_md, transcript_md = split_summary_transcript(body)
        summary_md = promote_section_headings(summary_md)

        date_str = meta.get("date", "")
        month = date_str[:7] if len(date_str) >= 7 else ""
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            date_display = f"{date_obj.strftime('%B')} {date_obj.day}, {date_obj.year}"
            month_label = f"{date_obj.strftime('%B')} {date_obj.year}"
        except (ValueError, AttributeError):
            date_display = date_str
            month_label = month

        committee_str = meta.get("committee", "")
        committee_list = [c.strip() for c in committee_str.split(" | ") if c.strip()]

        chairs_str = meta.get("chairs", "")
        members_str = meta.get("members", "")
        chairs_list = [c.strip() for c in chairs_str.split(" | ") if c.strip()]
        members_by_committee = [
            [m.strip() for m in segment.split(",") if m.strip()]
            for segment in members_str.split(" | ")
        ] if members_str else []
        seen = set(chairs_list)
        members_flat = []
        for sub in members_by_committee:
            for m in sub:
                if m and m not in seen:
                    seen.add(m)
                    members_flat.append(m)

        summary_plain = re.sub(r"^#{1,6}\s+.*$", "", summary_md, flags=re.MULTILINE)
        summary_plain = re.sub(r"[*_\[\]\(\)`>]", "", summary_plain)
        summary_plain = " ".join(summary_plain.split())

        hearings.append(
            {
                "committee": committee_str,
                "committee_list": committee_list,
                "committee_slug": meta.get("committee_slug", ""),
                "chairs": chairs_list,
                "members_by_committee": members_by_committee,
                "members_flat": members_flat,
                "title": meta.get("title", filename),
                "date": date_str,
                "date_display": date_display,
                "month": month,
                "month_label": month_label,
                "slug": meta.get("slug", filename.replace(".md", "")),
                "duration": meta.get("duration", ""),
                "youtube_url": meta.get("youtube_url", ""),
                "viebit_url": meta.get("viebit_url", ""),
                "council_url": meta.get("council_url", ""),
                "summary_html": markdown.markdown(summary_md),
                "summary_snippet": truncate_text(summary_plain, 160),
                "transcript_html": markdown.markdown(transcript_md)
                if transcript_md
                else "",
                "transcript_md": transcript_md,
                "transcript_text": markdown_to_text(transcript_md)
                if transcript_md
                else "",
            }
        )

    # Sort by date descending (newest first)
    hearings.sort(key=lambda h: h["date"], reverse=True)
    return hearings


def build():
    """Build the static site."""
    # Clean output
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)

    # Copy static files
    if os.path.exists(STATIC_DIR):
        shutil.copytree(STATIC_DIR, os.path.join(OUTPUT_DIR, "static"))

    # Set up Jinja2
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)

    hearings = load_content()

    # Filter facets for the index controls
    all_committees = sorted({c for h in hearings for c in h["committee_list"]})
    seen_months = {}
    for h in hearings:
        if h["month"] and h["month"] not in seen_months:
            seen_months[h["month"]] = h["month_label"]
    all_months = [
        {"value": v, "label": seen_months[v]}
        for v in sorted(seen_months.keys(), reverse=True)
    ]

    search_examples = select_search_examples(hearings)

    # Build index page
    index_template = env.get_template("index.html")
    index_html = index_template.render(
        hearings=hearings,
        committees=all_committees,
        months=all_months,
        search_examples=search_examples,
        meta_title="Hearing Hearings",
        meta_description="Summaries and transcripts of New York City Council hearings.",
        meta_url=f"{SITE_URL}/",
        meta_image=META_IMAGE,
    )
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"Built: index.html ({len(hearings)} hearings, {len(search_examples)} search examples)")

    # Build individual hearing pages
    hearing_template = env.get_template("hearing.html")
    meetings_dir = os.path.join(OUTPUT_DIR, "hearings")
    os.makedirs(meetings_dir)

    for hearing in hearings:
        hearing_dir = os.path.join(meetings_dir, hearing["slug"])
        os.makedirs(hearing_dir)
        combined_title = (
            f"{hearing['committee']}: {hearing['title']}"
            if hearing["committee"]
            else hearing["title"]
        )
        html = hearing_template.render(
            hearing=hearing,
            meta_title=combined_title,
            meta_description=hearing["summary_snippet"],
            meta_url=f"{SITE_URL}/hearings/{hearing['slug']}/",
            meta_image=META_IMAGE,
        )
        with open(os.path.join(hearing_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Built: hearings/{hearing['slug']}/index.html")

        if hearing["transcript_md"]:
            header_lines = [hearing["title"]]
            meta_bits = [b for b in (hearing["committee"], hearing["date_display"], hearing["duration"]) if b]
            if meta_bits:
                header_lines.append(" · ".join(meta_bits))
            header_lines.append(f"Source: {SITE_URL}/hearings/{hearing['slug']}/")
            if hearing["youtube_url"]:
                header_lines.append(f"Video: {hearing['youtube_url']}")
            transcript_txt = (
                "\n".join(header_lines)
                + "\n\n"
                + ("=" * 64)
                + "\n\n"
                + hearing["transcript_text"]
            )
            with open(os.path.join(hearing_dir, "transcript.txt"), "w", encoding="utf-8") as f:
                f.write(transcript_txt)

    build_search_index(hearings)

    # Build 404 page
    four04_template = env.get_template("404.html")
    four04_html = four04_template.render(
        meta_title="404 — Page Not Found | Hearing Hearings",
        meta_description="Page not found.",
        meta_url=f"{SITE_URL}/",
        meta_image=META_IMAGE,
    )
    with open(os.path.join(OUTPUT_DIR, "404.html"), "w", encoding="utf-8") as f:
        f.write(four04_html)
    print("Built: 404.html")

    # Generate sitemap.xml
    today = datetime.now().strftime("%Y-%m-%d")
    sitemap_entries = [
        f"  <url>\n    <loc>{SITE_URL}/</loc>\n    <lastmod>{today}</lastmod>\n  </url>"
    ]
    for h in hearings:
        sitemap_entries.append(
            f"  <url>\n    <loc>{SITE_URL}/hearings/{h['slug']}/</loc>\n    <lastmod>{h['date']}</lastmod>\n  </url>"
        )
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(sitemap_entries)
        + "\n</urlset>\n"
    )
    with open(os.path.join(OUTPUT_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap_xml)
    print("Built: sitemap.xml")

    # Generate robots.txt
    robots_txt = f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n"
    with open(os.path.join(OUTPUT_DIR, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(robots_txt)
    print("Built: robots.txt")

    # Generate Atom feed
    latest_date = hearings[0]["date"] if hearings else today
    feed_entries = []
    for h in hearings:
        feed_title = f"{h['committee']}, {h['title']}" if h.get("committee") else h["title"]
        feed_entries.append(
            f"  <entry>\n"
            f"    <title>{html_mod.escape(feed_title)}</title>\n"
            f"    <link href=\"{SITE_URL}/hearings/{h['slug']}/\" rel=\"alternate\"/>\n"
            f"    <id>{SITE_URL}/hearings/{h['slug']}/</id>\n"
            f"    <updated>{h['date']}T00:00:00Z</updated>\n"
            f"    <summary>{html_mod.escape(h['summary_snippet'])}</summary>\n"
            f"  </entry>"
        )
    feed_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>Hearing Hearings</title>\n"
        f"  <subtitle>Summaries and transcripts of New York City Council hearings.</subtitle>\n"
        f'  <link href="{SITE_URL}/feed.xml" rel="self"/>\n'
        f'  <link href="{SITE_URL}/" rel="alternate"/>\n'
        f"  <id>{SITE_URL}/</id>\n"
        f"  <updated>{latest_date}T00:00:00Z</updated>\n"
        + "\n".join(feed_entries)
        + "\n</feed>\n"
    )
    with open(os.path.join(OUTPUT_DIR, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed_xml)
    print("Built: feed.xml")

    print(f"\nSite built to {OUTPUT_DIR}")


if __name__ == "__main__":
    build()
