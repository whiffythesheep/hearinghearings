"""One-off backfill: add chairs/members YAML fields to existing hearing pages.

Walks content/*.md, finds each hearing's cached agenda PDF in Input/, re-runs
ONLY extract_agenda_metadata() to pick up the new chairs/members fields, and
patches the YAML front matter in-place. Does NOT touch summary or transcript.

Skips hearings that already have chairs: in their YAML, and any whose cached
agenda PDF is missing.

Usage:
    python backfill_chairs_members.py [--dry-run] [--only <slug>]
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from summarize_council_meeting import (
    parse_agenda,
    extract_agenda_metadata,
    build_committee_chair_lookup,
    supplement_chairs_via_lookup,
)

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR / "Input"
CONTENT_DIR = SCRIPT_DIR / "content"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill")


def parse_front_matter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text, None
    meta = {}
    for line in m.group(1).split("\n"):
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    return meta, m.group(2), m.group(1)


def find_cache_for_video_id(video_id):
    for path in INPUT_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("video_id") == video_id:
            return path, data
    return None, None


def find_cache_for_viebit_url(viebit_url):
    for path in INPUT_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("viebit_url") == viebit_url:
            return path, data
    return None, None


def patch_front_matter(md_text, chairs, members):
    """Insert chairs/members lines before the closing ---. Idempotent."""
    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)(.*)$", md_text, re.DOTALL)
    if not m:
        raise ValueError("No YAML front matter found")
    header = m.group(2)
    # Remove any existing chairs/members lines first (allow re-runs).
    header = re.sub(r"^chairs:.*$\n?", "", header, flags=re.MULTILINE)
    header = re.sub(r"^members:.*$\n?", "", header, flags=re.MULTILINE)
    header = header.rstrip("\n")
    additions = []
    if chairs:
        additions.append(f'chairs: "{chairs}"')
    if members:
        additions.append(f'members: "{members}"')
    if additions:
        header = header + "\n" + "\n".join(additions)
    return m.group(1) + header + m.group(3) + m.group(4)


def backfill_one(md_path, client, dry_run=False):
    md_text = md_path.read_text(encoding="utf-8")
    meta, _, _ = parse_front_matter(md_text)
    slug = meta.get("slug", md_path.stem)

    if meta.get("chairs"):
        logger.info(f"  Skip {slug}: chairs already present.")
        return "skipped"

    youtube_url = meta.get("youtube_url", "")
    viebit_url = meta.get("viebit_url", "")
    video_id = youtube_url.split("=")[-1] if youtube_url else None
    cache_path, cached_data = (None, None)
    if video_id:
        cache_path, cached_data = find_cache_for_video_id(video_id)
    if not cache_path and viebit_url:
        cache_path, cached_data = find_cache_for_viebit_url(viebit_url)
    if not cache_path:
        logger.warning(f"  Skip {slug}: no cached JSON found.")
        return "no_cache"

    agenda_name = cached_data.get("agenda_pdf")
    if not agenda_name:
        logger.warning(f"  Skip {slug}: cache has no agenda_pdf field.")
        return "no_agenda_field"

    agenda_path = INPUT_DIR / agenda_name
    if not agenda_path.exists():
        logger.warning(f"  Skip {slug}: agenda PDF missing at {agenda_path}.")
        return "no_pdf"

    logger.info(f"  {slug} -> {agenda_name}")
    agenda_text = parse_agenda(agenda_path)
    _, _, chairs, members = extract_agenda_metadata(agenda_text, client)
    logger.info(f"    chairs={chairs!r}")
    logger.info(f"    members={members!r}")

    if not chairs and not members:
        logger.warning(f"  Nothing extracted for {slug}; leaving unchanged.")
        return "empty_extraction"

    if dry_run:
        return "would_patch"

    new_text = patch_front_matter(md_text, chairs, members)
    md_path.write_text(new_text, encoding="utf-8")
    return "patched"


def deploy_site():
    logger.info("Building site...")
    site_dir = SCRIPT_DIR / "site"
    result = subprocess.run(
        [sys.executable, "build.py"],
        cwd=str(site_dir), capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(f"Build failed:\n{result.stderr}")
        return False
    logger.info("Build OK.")

    logger.info("Committing and pushing...")
    subprocess.run(["git", "add", "-A"], cwd=str(SCRIPT_DIR), check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "Backfill chair and member info on hearing pages"],
        cwd=str(SCRIPT_DIR), capture_output=True, text=True,
    )
    if commit.returncode != 0:
        if "nothing to commit" in commit.stdout:
            logger.info("No changes to commit.")
            return True
        logger.error(f"Commit failed:\n{commit.stderr}")
        return False
    subprocess.run(["git", "push"], cwd=str(SCRIPT_DIR), check=True)
    logger.info("Pushed.")
    return True


def supplement_one_from_lookup(md_path, lookup, dry_run=False):
    """Phase 2: fill in missing co-committee chairs on joint hearings via lookup."""
    md_text = md_path.read_text(encoding="utf-8")
    meta, _, _ = parse_front_matter(md_text)
    committees_str = meta.get("committee", "")
    chairs_str = meta.get("chairs", "")
    if not chairs_str:
        return "no_chairs"
    new_chairs_str = supplement_chairs_via_lookup(committees_str, chairs_str, lookup)
    if new_chairs_str == chairs_str:
        return "complete_or_unsupplementable"
    logger.info(f"  Supplemented {md_path.stem}: {chairs_str!r} -> {new_chairs_str!r}")
    if dry_run:
        return "would_supplement"
    new_text = patch_front_matter(md_text, new_chairs_str, meta.get("members", ""))
    md_path.write_text(new_text, encoding="utf-8")
    return "supplemented"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", type=str, default=None,
                        help="Backfill only the meeting with this slug.")
    parser.add_argument("--no-deploy", action="store_true",
                        help="Skip the build + git push step at the end.")
    parser.add_argument("--supplement-only", action="store_true",
                        help="Skip extraction phase; only run lookup-based "
                             "supplementation of joint-hearing chairs.")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.supplement_only:
        logger.error("ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key) if api_key else None

    md_paths = sorted(CONTENT_DIR.glob("*.md"))
    if args.only:
        md_paths = [p for p in md_paths if p.stem == args.only]
        if not md_paths:
            logger.error(f"No content file matches slug {args.only!r}.")
            sys.exit(1)

    counts = {}
    changed_any = False

    if not args.supplement_only:
        logger.info("=== Phase 1: extract chairs/members from agenda PDFs ===")
        for path in md_paths:
            logger.info(f"--- {path.name} ---")
            try:
                outcome = backfill_one(path, client, dry_run=args.dry_run)
            except Exception as e:
                logger.exception(f"  Error processing {path.name}: {e}")
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "patched":
                changed_any = True

    logger.info("=== Phase 2: supplement joint-hearing chairs from prior extractions ===")
    lookup = build_committee_chair_lookup(CONTENT_DIR)
    logger.info(f"  Lookup has {len(lookup)} known committee -> chair entries.")
    for path in md_paths:
        try:
            outcome = supplement_one_from_lookup(path, lookup, dry_run=args.dry_run)
        except Exception as e:
            logger.exception(f"  Error supplementing {path.name}: {e}")
            outcome = "supplement_error"
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "supplemented":
            changed_any = True

    logger.info(f"Summary: {counts}")

    if changed_any and not args.dry_run and not args.no_deploy:
        deploy_site()


if __name__ == "__main__":
    main()
