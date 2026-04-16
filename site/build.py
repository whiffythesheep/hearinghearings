"""Build static HTML site from markdown content files."""

import html as html_mod
import os
import re
import shutil
import markdown

from datetime import datetime
from jinja2 import Environment, FileSystemLoader

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR = os.path.join(os.path.dirname(ROOT), "content")
TEMPLATE_DIR = os.path.join(ROOT, "templates")
STATIC_DIR = os.path.join(ROOT, "static")
OUTPUT_DIR = os.path.join(ROOT, "output")
SITE_URL = "https://hearinghearings.nyc"


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


SECTION_LABELS_H2 = {"Summary"}
SECTION_LABELS_H3 = {"Meeting Overview", "Numbers", "Action Points"}


def promote_section_headings(md_text):
    """Convert bare section-label lines (e.g. 'Summary', 'Numbers') to markdown headings.

    The summarizer currently emits these as plain paragraphs. Promote them so
    templates can style them as a proper heading hierarchy.
    """
    out_lines = []
    for line in md_text.split("\n"):
        stripped = line.strip()
        if stripped in SECTION_LABELS_H2:
            out_lines.append(f"## {stripped}")
        elif stripped in SECTION_LABELS_H3:
            out_lines.append(f"### {stripped}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def split_summary_transcript(body):
    """Split markdown body into summary and transcript sections."""
    # Look for ## Full Transcript heading
    match = re.search(r"^## Full Transcript\s*$", body, re.MULTILINE)
    if match:
        summary = body[: match.start()].strip()
        transcript = body[match.end() :].strip()
        return summary, transcript
    return body, ""


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
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            date_display = f"{date_obj.strftime('%B')} {date_obj.day}, {date_obj.year}"
        except (ValueError, AttributeError):
            date_display = date_str

        summary_plain = re.sub(r"^#{1,6}\s+.*$", "", summary_md, flags=re.MULTILINE)
        summary_plain = re.sub(r"[*_\[\]\(\)`>]", "", summary_plain)
        summary_plain = " ".join(summary_plain.split())

        hearings.append(
            {
                "title": meta.get("title", filename),
                "date": date_str,
                "date_display": date_display,
                "slug": meta.get("slug", filename.replace(".md", "")),
                "duration": meta.get("duration", ""),
                "youtube_url": meta.get("youtube_url", ""),
                "council_url": meta.get("council_url", ""),
                "summary_html": markdown.markdown(summary_md),
                "summary_snippet": truncate_text(summary_plain, 160),
                "transcript_html": markdown.markdown(transcript_md)
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

    # Build index page
    index_template = env.get_template("index.html")
    index_html = index_template.render(
        hearings=hearings,
        meta_title="Hearing Hearings",
        meta_description="Summaries and transcripts of New York City Council hearings.",
        meta_url=f"{SITE_URL}/",
    )
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"Built: index.html ({len(hearings)} hearings)")

    # Build individual hearing pages
    hearing_template = env.get_template("hearing.html")
    meetings_dir = os.path.join(OUTPUT_DIR, "hearings")
    os.makedirs(meetings_dir)

    for hearing in hearings:
        hearing_dir = os.path.join(meetings_dir, hearing["slug"])
        os.makedirs(hearing_dir)
        html = hearing_template.render(
            hearing=hearing,
            meta_title=hearing["title"],
            meta_description=hearing["summary_snippet"],
            meta_url=f"{SITE_URL}/hearings/{hearing['slug']}/",
        )
        with open(os.path.join(hearing_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Built: hearings/{hearing['slug']}/index.html")

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
        feed_entries.append(
            f"  <entry>\n"
            f"    <title>{html_mod.escape(h['title'])}</title>\n"
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
