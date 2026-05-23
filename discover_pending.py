"""Poll Legistar for new hearings, run the pipeline, and open PRs.

For each newly-archived hearing on Calendar.aspx that we haven't
already published or queued, the poller:

1. Filters out stated meetings, vote-only sessions, and anything under
   60 minutes of caption duration (the Viebit VTT's last cue).
2. Creates a fresh ``pending/<event_id>`` branch off master.
3. Runs ``summarize_council_meeting.py --legistar-url ... --no-deploy``.
4. Builds the site, commits, pushes the branch, and opens a PR via
   ``gh``. Cloudflare Pages's branch-preview deployment becomes the
   review surface; merging the PR is the publish step.

Idempotency: an event is "handled" iff its Legistar ID appears in any
``content/*.md``'s ``council_url`` field on master, or an open PR
already exists for ``pending/<event_id>``. The script reads both
sources before queuing work.

CLI:

    python discover_pending.py            # list survivors, process up to --limit
    python discover_pending.py --dry-run  # just report what would happen
    python discover_pending.py --limit 3  # process up to 3 events
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from council_scraper import (
    fetch_viebit_duration_seconds,
    list_calendar_events,
)

REPO_ROOT = Path(__file__).resolve().parent
CONTENT_DIR = REPO_ROOT / "content"
SITE_BUILD_PY = REPO_ROOT / "site" / "build.py"
SUMMARIZER_PY = REPO_ROOT / "summarize_council_meeting.py"

# Anything matching these patterns in the body name is procedural and
# never gets a summary page. Case-insensitive substring.
BODY_SKIP_PATTERNS = ("stated meeting", "executive session")

# Location-cell <em> marker for vote-only sessions on Legistar. Both
# "VOTE" and "VOTE*" appear in the wild.
VOTE_EM_PREFIX = "VOTE"

MIN_DURATION_SECONDS = 60 * 60  # 1 hour

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("discover_pending")


# --- preconditions ------------------------------------------------------


def check_preconditions(dry_run: bool) -> None:
    """Bail early on missing tools, dirty tree, or wrong branch."""
    if not SUMMARIZER_PY.exists() or not SITE_BUILD_PY.exists():
        sys.exit("discover_pending.py must be run from the repo root.")
    if subprocess.run(
        ["gh", "--version"], capture_output=True, text=True,
    ).returncode != 0:
        sys.exit("`gh` CLI not found. Install from https://cli.github.com/.")
    auth = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True,
    )
    if auth.returncode != 0:
        sys.exit(f"gh not authenticated:\n{auth.stderr}")
    if dry_run:
        return
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if branch != "master":
        sys.exit(f"Expected master, currently on {branch}. Switch back first.")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        sys.exit(
            "Working tree is dirty — commit or stash before running the "
            "poller so pending branches start from a clean base."
        )


# --- idempotency sources ------------------------------------------------


_PUBLISHED_ID_RE = re.compile(
    r"^council_url:.*[?&]ID=(\d+)", re.MULTILINE | re.IGNORECASE,
)


def published_event_ids() -> set[str]:
    """Scan content/*.md front matter for already-published event IDs."""
    ids: set[str] = set()
    if not CONTENT_DIR.is_dir():
        return ids
    for md in CONTENT_DIR.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _PUBLISHED_ID_RE.finditer(text):
            ids.add(m.group(1))
    return ids


_PENDING_BRANCH_RE = re.compile(r"^pending/(\d+)\b")


def open_pr_event_ids() -> set[str]:
    """Event IDs that already have an open ``pending/*`` PR on GitHub."""
    res = subprocess.run(
        ["gh", "pr", "list", "--state", "open",
         "--search", "head:pending/", "--json", "headRefName"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if res.returncode != 0:
        logger.warning("gh pr list failed: %s", res.stderr.strip())
        return set()
    ids: set[str] = set()
    for pr in json.loads(res.stdout or "[]"):
        m = _PENDING_BRANCH_RE.match(pr.get("headRefName", ""))
        if m:
            ids.add(m.group(1))
    return ids


def local_pending_branch_event_ids() -> set[str]:
    """Event IDs with a local ``pending/*`` branch (from a previous failed run)."""
    res = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)",
         "refs/heads/pending/"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return set()
    ids: set[str] = set()
    for line in res.stdout.splitlines():
        m = _PENDING_BRANCH_RE.match(line.strip())
        if m:
            ids.add(m.group(1))
    return ids


# --- filtering ----------------------------------------------------------


def should_skip(event: dict) -> str | None:
    """Return a one-word skip reason, or None if the event should proceed."""
    if not (event["has_agenda"] and event["has_video"]):
        return "no-agenda-or-video"
    if not event["event_date"]:
        return "no-date"
    try:
        d = datetime.strptime(event["event_date"], "%Y-%m-%d").date()
    except ValueError:
        return "bad-date"
    if d > date.today():
        return "future"
    body_lower = event["body_name"].lower()
    if any(p in body_lower for p in BODY_SKIP_PATTERNS):
        return "stated/executive"
    if event["location_em"].upper().startswith(VOTE_EM_PREFIX):
        return "vote-only"
    return None


# --- pipeline drive -----------------------------------------------------


def run_pipeline(legistar_url: str) -> bool:
    """Invoke summarize_council_meeting.py for one event.

    Pipes ``y\\n`` to stdin so the optional agenda/video mismatch prompt
    auto-accepts — the YouTube resolver already gates on a Jaccard
    score and only the rare false positive trips that prompt.
    """
    cmd = [
        sys.executable, str(SUMMARIZER_PY),
        "--legistar-url", legistar_url,
        "--no-deploy",
    ]
    logger.info("Running pipeline: %s", " ".join(cmd[1:]))
    res = subprocess.run(
        cmd, cwd=REPO_ROOT, input="y\n", text=True,
    )
    if res.returncode != 0:
        logger.error("Pipeline exited with code %s", res.returncode)
        return False
    return True


def build_site() -> bool:
    res = subprocess.run(
        [sys.executable, str(SITE_BUILD_PY.name)],
        cwd=SITE_BUILD_PY.parent,
    )
    return res.returncode == 0


def new_markdown_files() -> list[Path]:
    """Untracked or modified content/*.md files since master."""
    res = subprocess.run(
        ["git", "status", "--porcelain", "content/"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    paths: list[Path] = []
    for line in res.stdout.splitlines():
        # Porcelain v1: two status chars, space, path.
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if path.endswith(".md"):
            paths.append(REPO_ROOT / path)
    return paths


# --- PR body ------------------------------------------------------------


def parse_front_matter(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {"_body": text}
    fm: dict = {"_body": m.group(2)}
    for line in m.group(1).splitlines():
        kv = re.match(r'(\w+):\s*"?(.*?)"?\s*$', line)
        if kv:
            fm[kv.group(1)] = kv.group(2)
    return fm


def summary_preview(body_md: str, max_chars: int = 600) -> str:
    """First ~600 chars of the rendered summary, lightly cleaned for phone reading."""
    # Skip the "Summary" h2 and any leading blank lines.
    text = re.sub(r"^#+\s.*?\n", "", body_md, count=1).strip()
    if len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        if cut > max_chars - 80:
            text = text[:cut] + " …"
        else:
            text = text[:max_chars] + " …"
    return text


def compose_pr_body(md_path: Path, event: dict) -> str:
    fm = parse_front_matter(md_path)
    committee = fm.get("committee", "?")
    title = fm.get("title", "?")
    duration = fm.get("duration", "?")
    youtube_url = fm.get("youtube_url", "")
    viebit_url = fm.get("viebit_url", "")
    council_url = fm.get("council_url", event["council_url"])
    video_link = youtube_url or viebit_url or "(none)"

    lines = [
        f"**{committee}** — {fm.get('date', event['event_date'])} · {duration}",
        f"_{title}_",
        "",
        f"- Video: {video_link}",
        f"- Council: {council_url}",
        "",
        "## Summary preview",
        "",
        summary_preview(fm.get("_body", "")),
        "",
        "---",
        "_Merge to publish. Cloudflare's preview deployment for this "
        "branch is the live review surface — open the link in the "
        "Cloudflare PR comment below._",
    ]
    return "\n".join(lines)


# --- branch + PR machinery ---------------------------------------------


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        check=check,
    )


def process_event(event: dict) -> str:
    """Run pipeline + open PR for one event. Returns status string."""
    branch = f"pending/{event['event_id']}"
    logger.info("=== %s | %s | %s",
                event["event_id"], event["event_date"], event["body_name"])

    git("checkout", "-b", branch)
    try:
        if not run_pipeline(event["council_url"]):
            return "pipeline-failed"
        new_md = new_markdown_files()
        if not new_md:
            logger.error("Pipeline succeeded but no new content/*.md found")
            return "no-output"
        md_path = new_md[0]
        if not build_site():
            return "site-build-failed"

        fm = parse_front_matter(md_path)
        commit_title = (
            f"Publish: {fm.get('committee', '?')}, {fm.get('title', '?')}"
        )

        git("add", "-A")
        commit = git("commit", "-m", commit_title, check=False)
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            logger.error("git commit failed: %s", commit.stderr)
            return "commit-failed"

        push = git("push", "-u", "origin", branch, check=False)
        if push.returncode != 0:
            logger.error("git push failed: %s", push.stderr)
            return "push-failed"

        pr_body = compose_pr_body(md_path, event)
        pr = subprocess.run(
            ["gh", "pr", "create",
             "--base", "master",
             "--head", branch,
             "--title", commit_title,
             "--body", pr_body],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if pr.returncode != 0:
            logger.error("gh pr create failed: %s", pr.stderr)
            return "pr-failed"
        logger.info("PR opened: %s", pr.stdout.strip())
        return "ok"
    finally:
        # Return to master no matter what so the next event starts clean.
        # If the branch has uncommitted work (we failed before commit),
        # leave it alone — user inspects manually.
        co = git("checkout", "master", check=False)
        if co.returncode != 0:
            logger.warning(
                "Could not return to master cleanly (branch %s left in "
                "place for inspection): %s", branch, co.stderr.strip(),
            )


# --- main ---------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="List candidates and skip reasons; make no git/network changes "
             "beyond reading the calendar and duration probes.",
    )
    p.add_argument(
        "--limit", type=int, default=1,
        help="Maximum events to process this run (default: 1). Each event "
             "costs ~$0.30 and takes ~5–10 minutes.",
    )
    p.add_argument(
        "--no-duration-check", action="store_true",
        help="Skip the Viebit duration probe (saves ~5s per candidate). "
             "Use only when the title/VOTE filters are known to be enough.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    check_preconditions(args.dry_run)

    events = list_calendar_events()
    published = published_event_ids()
    pr_open = open_pr_event_ids()
    local_pending = local_pending_branch_event_ids()

    logger.info(
        "Calendar=%d | published=%d | open PRs=%d | local pending branches=%d",
        len(events), len(published), len(pr_open), len(local_pending),
    )

    candidates: list[dict] = []
    skipped: dict[str, int] = {}
    for e in events:
        if e["event_id"] in published:
            skipped["already-published"] = skipped.get("already-published", 0) + 1
            continue
        if e["event_id"] in pr_open:
            skipped["pr-open"] = skipped.get("pr-open", 0) + 1
            continue
        if e["event_id"] in local_pending:
            skipped["local-pending"] = skipped.get("local-pending", 0) + 1
            continue
        reason = should_skip(e)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        candidates.append(e)

    # Duration check (slow-ish; do after cheap filters).
    survivors: list[dict] = []
    if args.no_duration_check:
        survivors = candidates
    else:
        for e in candidates:
            secs = fetch_viebit_duration_seconds(e["video_url"])
            if secs is None:
                # Unknown duration — let it through; the pipeline itself
                # will surface any caption-source issues.
                survivors.append(e)
                continue
            if secs < MIN_DURATION_SECONDS:
                skipped["under-1h"] = skipped.get("under-1h", 0) + 1
                logger.info(
                    "  drop %s (%s) — duration %d:%02d under threshold",
                    e["event_id"], e["body_name"][:40],
                    secs // 60, secs % 60,
                )
                continue
            e["_duration_s"] = secs
            survivors.append(e)

    logger.info("Skipped tally: %s", skipped)
    logger.info("Survivors: %d", len(survivors))
    for e in survivors:
        dur = e.get("_duration_s")
        dur_s = f" ({dur // 60}m)" if dur else ""
        logger.info(
            "  %s | %s | %s%s",
            e["event_id"], e["event_date"], e["body_name"][:50], dur_s,
        )

    if args.dry_run:
        logger.info("Dry run — no branches or PRs created.")
        return

    if not survivors:
        logger.info("Nothing to do.")
        return

    processed = 0
    statuses: list[tuple[str, str]] = []
    for e in survivors:
        if processed >= args.limit:
            logger.info(
                "Hit --limit %d. %d candidates remain for a later run.",
                args.limit, len(survivors) - processed,
            )
            break
        status = process_event(e)
        statuses.append((e["event_id"], status))
        processed += 1

    logger.info("Done. Outcomes: %s", statuses)


if __name__ == "__main__":
    main()
