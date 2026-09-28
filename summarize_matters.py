#!/usr/bin/env python3
"""Plain-English titles and short summaries for every matter.

Legistar's titles are statutory boilerplate ("A Local Law to amend the
administrative code of the city of New York, in relation to prohibiting the
operation of class 3 e-bicycles"). This writes one simple title ("Law to ban
the fastest e-bikes") and a summary of at most three sentences per matter,
drawn from the matter's own Legistar documents: the Council's one-page bill
summary, the bill or resolution text, and the committee report.

Output: ``data/matter_plain/<slug>.json`` (tracked), one per matter, holding
the title, summary and a hash of the inputs. A matter is only regenerated
when its source documents, the prompt or the model change, so the nightly
run pays for new and amended matters only.

    python summarize_matters.py --count            # free: fetch docs, count tokens, estimate cost
    python summarize_matters.py --sample 10        # a few, synchronously, printed for review
    python summarize_matters.py --batch            # everything missing, via the Batch API (half price)
    python summarize_matters.py --collect <id>     # write the results of a finished batch
    python summarize_matters.py --new              # nightly: missing or changed, synchronously
"""

import argparse
import glob
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from scrape_record import DATA_DIR, Fetcher, attachment_text, write_json

load_dotenv()

MODEL = "claude-sonnet-5"
PRICE_IN, PRICE_OUT = 2.00, 10.00          # $ per million tokens, claude-sonnet-5
PROMPT_VERSION = 1                          # bump to regenerate everything
OUT_DIR = DATA_DIR / "matter_plain"
NIGHTLY_LIMIT = 60                          # safety cap on synchronous calls per night

# Procedural paperwork the site leaves out (see site/record.py).
EXCLUDED_TYPES = {"Communication", "Mayor's Message", "Commissioner of Deeds", "N/A"}

# Which documents to read, in order, and how much of each. The Council's own
# one-page summary is the best source when it exists; committee reports run
# to dozens of pages but open with the background and purpose, so the leading
# section carries what a three-sentence summary needs.
SOURCES = [("Summary", 8000), ("Bill text", 12000), ("Committee Report", 20000)]

SYSTEM = """You write plain-English labels for New York City Council matters, for a public website read by non-specialists.

For each matter you get its Legistar type, official title and excerpts from its documents. Return:

title: a short, uniform title of the form "[Kind] to [do something]", at most 10 words.
- Kind is "Law" for an Introduction, "Resolution" for a Resolution, "Hearing" for an Oversight topic ("Hearing on ..." is fine there), and for land use "Rezoning", "Landmark designation", "Application" or similar.
- Say what it does in everyday words, not statutory ones. "Law to ban the fastest e-bikes", not "Law to prohibit the operation of class 3 e-bicycles".
- For a resolution calling on another government, say who and what: "Resolution urging Albany to fund free school meals".
- Name a place when a land use matter is about one: "Rezoning to allow housing at 200 Kent Avenue, Brooklyn".
- One project often has several land use applications. Make each title distinct by saying which part it is: "Zoning map change for Monitor Point, Greenpoint" vs "Zoning text change for Monitor Point, Greenpoint" vs "Special permit for ...".
- No file numbers, no "A Local Law to amend", no jargon or acronyms a resident would not know.

summary: at most three sentences. What the matter would do or examine, who it affects, and any key number or date that is in the documents. Only state what the documents say. Do not speculate about its chances, politics or impact. If the documents are thin, write one sentence.

Style: American spelling. No Oxford commas. Periods rather than semicolons. Capitalise Council and City when they mean the New York City Council and City."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["title", "summary"],
    "additionalProperties": False,
}

logger = logging.getLogger("plain")


def load_matters():
    out = []
    for path in sorted(glob.glob(str(DATA_DIR / "matters" / "*.json"))):
        m = json.loads(Path(path).read_text(encoding="utf-8"))
        if m.get("type") not in EXCLUDED_TYPES:
            out.append(m)
    return out


def source_text(fetcher, matter):
    """The document excerpts sent to the model, newest version of each kind."""
    parts = []
    for kind, limit in SOURCES:
        doc = next((a for a in matter.get("attachments") or [] if a.get("kind") == kind), None)
        if not doc:
            continue
        try:
            text = attachment_text(fetcher.get_bytes(doc["url"])) or ""
        except Exception as e:  # one unreadable document should not stop the run
            logger.warning("  %s: %s unreadable (%s)", matter["file_number"], kind, e)
            continue
        text = " ".join(text.split())
        if text:
            parts.append(f"--- {kind}: {doc.get('label', '')} ---\n{text[:limit]}")
    return "\n\n".join(parts)


def user_message(matter, docs):
    head = (f"Type: {matter.get('type', '')}\n"
            f"File: {matter['file_number']}\n"
            f"Official title: {matter.get('title') or matter.get('name') or ''}\n")
    return head + "\n" + (docs or "(No documents available. Work from the title.)")


def params(matter, docs):
    return {
        "model": MODEL,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user_message(matter, docs)}],
        "output_config": {"effort": "low",
                          "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def input_hash(matter, docs):
    key = f"{PROMPT_VERSION}|{MODEL}|{SYSTEM}|{user_message(matter, docs)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def existing(slug):
    path = OUT_DIR / f"{slug}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def parse(message):
    if message.stop_reason != "end_turn":
        raise ValueError(f"stop_reason {message.stop_reason}")
    text = next((b.text for b in message.content if b.type == "text"), "")
    out = json.loads(text)
    return out["title"].strip(), out["summary"].strip()


def save(matter, h, title, summary):
    write_json(OUT_DIR / f"{matter['slug']}.json", {
        "file_number": matter["file_number"], "title": title, "summary": summary,
        "model": MODEL, "input_hash": h,
    })


def pending(fetcher, matters):
    """(matter, docs, hash) for every matter whose output is missing or stale."""
    out = []
    for i, m in enumerate(matters, 1):
        docs = source_text(fetcher, m)
        h = input_hash(m, docs)
        prev = existing(m["slug"])
        if not prev or prev.get("input_hash") != h:
            out.append((m, docs, h))
        if i % 50 == 0:
            logger.info("  checked %d/%d", i, len(matters))
    return out


def cmd_count(client, fetcher, matters):
    todo = pending(fetcher, matters)
    total = 0
    for m, docs, _h in todo:
        p = params(m, docs)
        n = client.messages.count_tokens(model=MODEL, system=p["system"],
                                         messages=p["messages"]).input_tokens
        total += n
    out_est = 600 * len(todo)  # title + three sentences + low-effort thinking
    cost = total / 1e6 * PRICE_IN + out_est / 1e6 * PRICE_OUT
    logger.info("Matters to process: %d of %d", len(todo), len(matters))
    logger.info("Input tokens (exact): %d, avg %d per matter", total, total // max(1, len(todo)))
    logger.info("Output tokens (estimate): %d", out_est)
    logger.info("Cost: ~$%.2f standard, ~$%.2f via the Batch API", cost, cost / 2)


def cmd_sync(client, fetcher, matters, limit, spread=False):
    todo = pending(fetcher, matters)
    if spread and todo:
        # A varied sample: cycle through the matter types.
        by_type = {}
        for t in todo:
            by_type.setdefault(t[0].get("type"), []).append(t)
        picked = []
        while len(picked) < limit and any(by_type.values()):
            for lst in by_type.values():
                if lst and len(picked) < limit:
                    picked.append(lst.pop(0))
        todo = picked
    else:
        todo = todo[:limit]
    used_in = used_out = 0
    for m, docs, h in todo:
        try:
            msg = client.messages.create(**params(m, docs))
            title, summary = parse(msg)
        except (anthropic.APIError, ValueError, KeyError) as e:
            logger.warning("  %s: %s", m["file_number"], e)
            continue
        used_in += msg.usage.input_tokens
        used_out += msg.usage.output_tokens
        save(m, h, title, summary)
        print(f"\n{m['file_number']} ({m.get('type')})\n  was:  {(m.get('title') or '')[:150]}"
              f"\n  now:  {title}\n  {summary}")
    cost = used_in / 1e6 * PRICE_IN + used_out / 1e6 * PRICE_OUT
    logger.info("Wrote %d. Tokens in %d, out %d. Cost $%.3f", len(todo), used_in, used_out, cost)


def cmd_batch(client, fetcher, matters):
    todo = pending(fetcher, matters)
    if not todo:
        logger.info("Nothing to do.")
        return
    batch = client.messages.batches.create(requests=[
        {"custom_id": m["slug"], "params": params(m, docs)} for m, docs, _h in todo])
    logger.info("Submitted batch %s with %d requests. Collect with --collect %s",
                batch.id, len(todo), batch.id)


def cmd_collect(client, fetcher, matters, batch_id):
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            break
        logger.info("  %s: %d processing", b.processing_status, b.request_counts.processing)
        time.sleep(60)
    by_slug = {m["slug"]: m for m in matters}
    ok = failed = 0
    used_in = used_out = 0
    for r in client.messages.batches.results(batch_id):
        m = by_slug.get(r.custom_id)
        if m is None or r.result.type != "succeeded":
            failed += 1
            continue
        try:
            title, summary = parse(r.result.message)
        except (ValueError, KeyError):
            failed += 1
            continue
        docs = source_text(fetcher, m)  # cached; recomputes the same hash
        save(m, input_hash(m, docs), title, summary)
        used_in += r.result.message.usage.input_tokens
        used_out += r.result.message.usage.output_tokens
        ok += 1
    cost = (used_in / 1e6 * PRICE_IN + used_out / 1e6 * PRICE_OUT) / 2
    logger.info("Wrote %d, failed %d. Batch cost ~$%.2f", ok, failed, cost)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--count", action="store_true")
    g.add_argument("--sample", type=int, metavar="N")
    g.add_argument("--batch", action="store_true")
    g.add_argument("--collect", metavar="BATCH_ID")
    g.add_argument("--new", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = anthropic.Anthropic()
    fetcher = Fetcher()
    matters = load_matters()
    if args.count:
        cmd_count(client, fetcher, matters)
    elif args.sample:
        cmd_sync(client, fetcher, matters, args.sample, spread=True)
    elif args.batch:
        cmd_batch(client, fetcher, matters)
    elif args.collect:
        cmd_collect(client, fetcher, matters, args.collect)
    elif args.new:
        cmd_sync(client, fetcher, matters, NIGHTLY_LIMIT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
