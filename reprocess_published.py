"""Batch-reprocess every meeting currently published on hearinghearings.nyc.

Walks content/, matches each published page to its cached transcript JSON
(by YouTube video_id) and to its agenda PDF, drops stale segmentation/cleaning
cache keys, and re-runs the summarizer pipeline via --transcript-json so
YouTube isn't re-fetched. Each meeting is run with --no-deploy; the site is
built and pushed once at the end.

Usage:
    python reprocess_published.py
    python reprocess_published.py --only <slug>
    python reprocess_published.py --dry-run
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR / "Input"
WEBSITE_DIR = SCRIPT_DIR
WEBSITE_CONTENT_DIR = WEBSITE_DIR / "content"
SUMMARIZE_SCRIPT = SCRIPT_DIR / "summarize_council_meeting.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("reprocess")


def parse_yaml_front_matter(md_path):
    """Return a dict of YAML front-matter fields from a markdown file."""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fields = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"(\w+):\s*(.*)", line)
        if not kv:
            continue
        key = kv.group(1)
        val = kv.group(2).strip().strip('"')
        fields[key] = val
    return fields


def find_cache_for_video_id(video_id):
    """Locate the cached transcript JSON whose video_id matches.

    Returns (path, data) or (None, None).
    """
    for path in INPUT_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("video_id") == video_id:
            return path, data
    return None, None


def drop_stale_cache_keys(json_path):
    """Remove keys that are tied to old segmentation output."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    removed = []
    for key in ("utterances", "speaker_map", "cleaned_utterances", "segmentation_version"):
        if key in data:
            data.pop(key)
            removed.append(key)
    if removed:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"  Dropped cache keys: {', '.join(removed)}")
    else:
        logger.info("  No stale cache keys to drop.")


def derive_user_title(full_title):
    """Strip the leading committee name from a published page title.

    Page titles are constructed as "{committee_name}, {user_title}". The
    summarizer rebuilds the same shape from --title + the committee name
    extracted from the agenda PDF, so we just need the part after the
    first ", ".
    """
    if ", " in full_title:
        return full_title.split(", ", 1)[1]
    return full_title


def reprocess_meeting(slug, page_fields, dry_run=False):
    title = page_fields.get("title", slug)
    youtube_url = page_fields.get("youtube_url", "")
    video_id = youtube_url.split("=")[-1] if youtube_url else None
    if not video_id:
        logger.error(f"  No youtube_url in page front matter; cannot match cache.")
        return False

    cache_path, cached_data = find_cache_for_video_id(video_id)
    if not cache_path:
        logger.error(f"  No cached JSON found for video_id {video_id}.")
        return False
    logger.info(f"  Cache: {cache_path.name}")

    agenda_name = cached_data.get("agenda_pdf")
    if not agenda_name:
        logger.error(
            f"  No 'agenda_pdf' field in {cache_path.name}. "
            f"Run the summarizer once for this meeting "
            f"(python summarize_council_meeting.py --transcript-json {cache_path} <agenda.pdf> --no-deploy) "
            f"to backfill it."
        )
        return False
    agenda_path = INPUT_DIR / agenda_name
    if not agenda_path.exists():
        logger.error(f"  Agenda PDF not found: {agenda_path}")
        return False
    logger.info(f"  Agenda: {agenda_path.name}")

    user_title = derive_user_title(title)
    logger.info(f"  Title arg: {user_title!r}")

    if dry_run:
        logger.info("  [dry-run] would drop cache keys and run summarizer.")
        return True

    drop_stale_cache_keys(cache_path)

    # Snapshot existing markdown files so we can detect any new file the
    # summarizer creates under a different slug.
    pre_existing = {p.name for p in WEBSITE_CONTENT_DIR.glob("*.md")}

    cmd = [
        sys.executable,
        str(SUMMARIZE_SCRIPT),
        "--transcript-json", str(cache_path),
        str(agenda_path),
        "--title", user_title,
        "--no-deploy",
    ]
    logger.info(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR.parent))
    if result.returncode != 0:
        logger.error(f"  Summarizer exited with code {result.returncode}")
        return False

    # Slug stability check: if the summarizer wrote to a different slug
    # (because Claude extracted a slightly different committee name), move
    # the new file back to the original slug and rewrite its YAML to match.
    expected_path = WEBSITE_CONTENT_DIR / f"{slug}.md"
    new_files = {p.name for p in WEBSITE_CONTENT_DIR.glob("*.md")} - pre_existing
    if new_files:
        # The summarizer created at least one new file -> slug drift.
        for new_name in new_files:
            new_path = WEBSITE_CONTENT_DIR / new_name
            logger.warning(
                f"  Slug drift: summarizer wrote {new_name}, expected {slug}.md"
            )
            content = new_path.read_text(encoding="utf-8")
            # Rewrite title and slug to match the original page
            content = re.sub(
                r'^title:\s*".*?"', f'title: "{title}"', content, count=1, flags=re.MULTILINE
            )
            content = re.sub(
                r'^slug:\s*\S+', f'slug: {slug}', content, count=1, flags=re.MULTILINE
            )
            expected_path.write_text(content, encoding="utf-8")
            new_path.unlink()
            logger.info(f"  Restored slug -> {expected_path.name}")
    return True


def deploy_site():
    """Build the site once and push all republished pages in a single commit."""
    logger.info("Building site...")
    site_dir = WEBSITE_DIR / "site"
    result = subprocess.run(
        [sys.executable, "build.py"],
        cwd=str(site_dir),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(f"Build failed:\n{result.stderr}")
        return False
    logger.info("Build OK.")

    logger.info("Committing and pushing...")
    subprocess.run(["git", "add", "-A"], cwd=str(WEBSITE_DIR), check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "Reprocess all meetings with fixed segmentation"],
        cwd=str(WEBSITE_DIR), capture_output=True, text=True,
    )
    if commit.returncode != 0:
        if "nothing to commit" in commit.stdout:
            logger.info("No changes to commit.")
            return True
        logger.error(f"Commit failed:\n{commit.stderr}")
        return False
    subprocess.run(["git", "push"], cwd=str(WEBSITE_DIR), check=True)
    logger.info("Pushed.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", type=str, default=None,
                        help="Reprocess only the meeting with this slug.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without running anything.")
    parser.add_argument("--no-deploy", action="store_true",
                        help="Skip the final build + git push step.")
    args = parser.parse_args()

    if not WEBSITE_CONTENT_DIR.exists():
        logger.error(f"Website content directory not found: {WEBSITE_CONTENT_DIR}")
        sys.exit(1)

    pages = sorted(WEBSITE_CONTENT_DIR.glob("*.md"))
    if args.only:
        pages = [p for p in pages if p.stem == args.only]
        if not pages:
            logger.error(f"No published page with slug {args.only}")
            sys.exit(1)

    logger.info(f"Found {len(pages)} published page(s) to reprocess.")

    succeeded = []
    failed = []
    for page in pages:
        slug = page.stem
        logger.info(f"=== {slug} ===")
        fields = parse_yaml_front_matter(page)
        ok = reprocess_meeting(slug, fields, dry_run=args.dry_run)
        (succeeded if ok else failed).append(slug)

    logger.info(f"Reprocessed {len(succeeded)} meetings successfully.")
    if failed:
        logger.error(f"Failed: {failed}")

    if args.dry_run or args.no_deploy or not succeeded:
        return

    deploy_site()


if __name__ == "__main__":
    main()
