"""Add `viebit_hash` to published hearings that lack it.

Timestamp deep links must point at Viebit's `/watch?hash=<hash>` page: the
`/vod/?s=true&v=<file>.mp4` shape stored in most hearings' front matter is a
redirector that 302s to `/embed/vod?v=<hash>` and rebuilds the query string
from scratch, silently dropping `?t=` so the video opens at 0:00.

`viebit_url` itself is deliberately left alone — `discover_pending.py`
fingerprints published hearings on the URL shape Legistar advertised, and the
two shapes are not interconvertible, so rewriting it would break joint-hearing
sibling dedup. The hash is added as a separate field instead.

The hash comes from the cached transcript JSON's caption URL where available
(free, offline); otherwise it is resolved by following the /vod/ redirect.

    python backfill_viebit_hashes.py --dry-run
    python backfill_viebit_hashes.py
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
INPUT_DIR = ROOT / "Input"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

HASH_RE = re.compile(r"[?&]hash=([A-Za-z0-9_-]+)")
CAPTION_HASH_RE = re.compile(r"/counciln/([A-Za-z0-9_-]+)/")
EMBED_HASH_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]+)")


def read_front_matter(text):
    """Return (dict, raw_block) for the leading YAML block, or ({}, "")."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if not m:
        return {}, ""
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    return meta, m.group(0)


def hash_from_cache(viebit_url):
    """Recover the hash from a cached transcript JSON's caption URL."""
    if not INPUT_DIR.is_dir():
        return ""
    for path in INPUT_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or data.get("viebit_url") != viebit_url:
            continue
        if data.get("player_hash"):
            return data["player_hash"]
        m = CAPTION_HASH_RE.search(data.get("caption_url", ""))
        if m:
            return m.group(1)
    return ""


def hash_from_network(viebit_url):
    """Follow the /vod/ redirect and read the hash off the landing URL."""
    try:
        r = requests.get(viebit_url, headers=BROWSER_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"    fetch failed: {e}")
        return ""
    final = r.url
    m = EMBED_HASH_RE.search(final) or CAPTION_HASH_RE.search(r.text)
    if m:
        return m.group(1)
    print(f"    no hash in landing URL: {final}")
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    added = skipped = failed = 0
    for path in sorted(CONTENT_DIR.glob("*.md")):
        # newline="" so the file's existing CRLF endings survive the rewrite.
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        meta, block = read_front_matter(text)
        viebit_url = meta.get("viebit_url", "")
        if not viebit_url or meta.get("viebit_hash"):
            continue
        if HASH_RE.search(viebit_url):
            skipped += 1  # already a /watch?hash= URL; links work as-is
            continue

        print(f"{path.name}")
        viebit_hash = hash_from_cache(viebit_url)
        source = "cache"
        if not viebit_hash:
            viebit_hash = hash_from_network(viebit_url)
            source = "network"
        if not viebit_hash:
            print("    UNRESOLVED")
            failed += 1
            continue
        print(f"    {viebit_hash}  ({source})")
        added += 1
        if args.dry_run:
            continue

        eol = "\r\n" if "\r\n" in block else "\n"
        line = f'viebit_hash: "{viebit_hash}"{eol}'
        anchor = f'viebit_url: "{viebit_url}"{eol}'
        if anchor not in block:
            print("    could not locate viebit_url line; skipped")
            failed += 1
            added -= 1
            continue
        text = text.replace(block, block.replace(anchor, anchor + line), 1)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb} viebit_hash to {added} file(s); "
          f"{skipped} already on /watch?hash=; {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
