"""Build static HTML site from markdown content files."""

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
    index_html = index_template.render(hearings=hearings)
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
        html = hearing_template.render(hearing=hearing)
        with open(os.path.join(hearing_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Built: hearings/{hearing['slug']}/index.html")

    print(f"\nSite built to {OUTPUT_DIR}")


if __name__ == "__main__":
    build()
