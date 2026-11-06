"""Temporary probe: can GitHub Actions find recordings faster via YouTube or
Viebit's catalogue than via Legistar's video link? Read-only; prints only."""
import json, sys, traceback, urllib.request
from datetime import date
sys.path.insert(0, ".")
from council_scraper import list_calendar_events
from discover_pending import should_skip

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}

events = list_calendar_events()
print(f"calendar rows: {len(events)}")
past = [e for e in events if e["event_date"] and e["event_date"] <= date.today().isoformat()]
for e in sorted(past, key=lambda e: e["event_date"]):
    print(f"{e['event_id']} {e['event_date']} agenda={int(e['has_agenda'])} video={int(e['has_video'])} "
          f"em={e['location_em']!r:10} skip={should_skip(e)} | {e['body_name'][:60]} | {e['video_url'] or ''}")

print("\n=== Viebit public catalogue ===")
for url in ["https://councilnyc.viebit.com/vb/public/vod",
            "https://councilnyc.viebit.com/vb/public/vod?page=1&limit=50"]:
    try:
        req = urllib.request.Request(url, headers=UA)
        body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        print(url, "->", len(body), "bytes")
        print(body[:3000])
    except Exception as ex:
        print(url, "FAILED", repr(ex))

print("\n=== YouTube ===")
from video_resolver import find_youtube_match
targets = [e for e in past if should_skip(e) == "no-video"]
targets += [e for e in past if e["event_id"] in ("1456023", "1454901", "1449028")]  # no-captions pair + a control
seen = set()
for e in targets:
    if e["event_id"] in seen: continue
    seen.add(e["event_id"])
    try:
        m = find_youtube_match(e["event_date"], e["body_name"])
        print(f"{e['event_id']} {e['event_date']} {e['body_name'][:50]} -> {m}")
    except Exception as ex:
        print(f"{e['event_id']} resolver FAILED {ex!r}")
        m = None
    if m:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            vid = m["url"].split("v=")[-1].split("&")[0]
            t = YouTubeTranscriptApi().fetch(vid)
            print(f"   transcript OK: {len(t.snippets)} snippets; first: {t.snippets[0].text!r}")
        except Exception as ex:
            print(f"   transcript FAILED: {type(ex).__name__}: {str(ex)[:300]}")
