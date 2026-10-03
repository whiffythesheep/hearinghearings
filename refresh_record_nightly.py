"""Nightly record refresh: re-scrape what can change, rebuild, push.

Runs from ``run_discover.ps1`` *before* ``discover_pending.py``, so new
``pending/`` branches are cut from an up-to-date master.

1. Sync master to ``origin/master`` (tree must be clean).
2. ``scrape_record.py --refresh``: re-fetches meetings from the last week,
   the next day's meetings (their agendas become the site's "Upcoming"
   rows), and every matter whose status is not yet settled. Everything
   else comes from the HTML cache, so a run is a few hundred requests.
3. ``summarize_matters.py --new``: plain-English titles and summaries for
   new or amended matters (Claude Sonnet 5, a few cents a night).
4. Rebuild the site. Commit and push to master only if ``data/`` changed.
5. Bring each open ``pending/*`` PR branch up to date with master and
   rebuild it. Both sides regenerate ``site/output/``, so without this an
   open PR would conflict with master on every page the refresh touched.

Any failure resets the working tree to where it started, so a bad night
never strands a dirty tree for ``discover_pending.py`` to trip over.

    python refresh_record_nightly.py
    python refresh_record_nightly.py --no-push     # local dry run
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

logger = logging.getLogger("refresh")

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, check=check)


def run(*args: str) -> bool:
    res = subprocess.run([PYTHON, *args], cwd=REPO_ROOT)
    return res.returncode == 0


def build() -> bool:
    return run("site/build.py")


def dirty(*paths: str) -> bool:
    return bool(git("status", "--porcelain", "--", *paths).stdout.strip())


def restore(sha: str) -> None:
    """Put the tree back exactly as it was at the start of the run."""
    git("merge", "--abort", check=False)
    git("checkout", "-f", "master", check=False)
    git("reset", "--hard", sha, check=False)
    git("clean", "-fdq", "--", "data", "site/output", check=False)


def open_pending_branches() -> list[str]:
    res = subprocess.run(
        # No --search: GitHub's search index can silently return nothing
        # (it did while the account was flagged). List and filter instead.
        ["gh", "pr", "list", "--state", "open", "--limit", "200",
         "--json", "headRefName"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        logger.warning("gh pr list failed, skipping PR branches: %s", res.stderr.strip())
        return []
    return [pr["headRefName"] for pr in json.loads(res.stdout or "[]")
            if pr.get("headRefName", "").startswith("pending/")]


def refresh_branch(branch: str, push: bool) -> None:
    """Merge master into a PR branch, regenerating site/output."""
    git("fetch", "origin", branch, check=False)
    git("checkout", "-B", branch, f"origin/{branch}")
    merge = git("merge", "--no-edit", "master", check=False)
    if merge.returncode != 0:
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        if any(not p.startswith("site/output/") for p in conflicted):
            logger.warning("%s: conflicts outside site/output (%s), left as is",
                           branch, ", ".join(p for p in conflicted
                                             if not p.startswith("site/output/")))
            git("merge", "--abort")
            return
        # Generated files only: take either side, the rebuild replaces them.
        git("checkout", "--theirs", "--", "site/output", check=False)
    if not build():
        raise RuntimeError(f"build failed on {branch}")
    git("add", "-A", "site/output")
    in_merge = (REPO_ROOT / ".git" / "MERGE_HEAD").exists()
    if in_merge:
        git("commit", "--no-edit")
    elif dirty("site/output"):
        git("commit", "-m", "Rebuild site after nightly record refresh")
    else:
        logger.info("%s: already up to date", branch)
        return
    if push:
        git("push", "origin", branch)
    logger.info("%s: merged master and rebuilt", branch)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-push", action="store_true", help="commit locally, push nothing")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    push = not args.no_push

    if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "master":
        logger.error("Not on master; skipping the record refresh.")
        return 1
    if dirty():
        logger.error("Working tree is dirty; skipping the record refresh.")
        return 1
    if push:
        git("fetch", "origin", "master", check=False)
        git("reset", "--hard", "origin/master")
    start = git("rev-parse", "HEAD").stdout.strip()

    try:
        if not run("scrape_record.py", "--refresh"):
            raise RuntimeError("scrape_record.py failed")
        # Plain-English titles and summaries for new or amended matters.
        # Costs a few cents a night; a failure leaves Legistar's titles showing.
        if not run("summarize_matters.py", "--new"):
            logger.warning("summarize_matters.py failed; continuing without new summaries")
        if dirty("data"):
            if not build():
                raise RuntimeError("build failed")
            git("add", "-A", "data", "site/output")
            git("commit", "-m", f"Nightly record refresh {date.today().isoformat()}")
            if push:
                git("push", "origin", "master")
            logger.info("Record refreshed and committed.")
        else:
            logger.info("No record changes.")

        for branch in open_pending_branches():
            try:
                refresh_branch(branch, push)
            finally:
                git("checkout", "-f", "master", check=False)
    except Exception as exc:  # noqa: BLE001 -- always leave a clean tree
        logger.error("Refresh failed: %s", exc)
        restore(start)
        return 1

    git("checkout", "-f", "master", check=False)
    return 0



if __name__ == "__main__":
    sys.exit(main())
