# Hearing Hearings

A static site summarising NYC Council hearings, paired with a Python pipeline that turns a YouTube recording + PDF agenda into a published summary page.

Personal project (not CBC). Deployed at [hearinghearings.nyc](https://hearinghearings.nyc).

## Relationship to hownycworks.org

Originally this product lived at `hownycworks.org`. In April 2026 it was spun out into its own self-contained repo + domain so it could be developed independently (subscription forms, search, filters, etc.) without coupling to the `hownycworks` umbrella.

Today:
- **hearinghearings.nyc** — this repo. The hearing summaries product. Everything here.
- **hownycworks.org** — a thin landing page listing projects, of which Hearing Hearings is the first. Separate repo at `C:\Users\seane\Documents\hownycworks\`. Frozen until new projects are added.

The two sites share the Win95/XP-inspired design system (originally built in hownycworks and copied over), but have independent repos, Cloudflare Pages projects, deployments, and issue tracking.

## Directory layout

```
hearinghearings/
├── .env                        # ANTHROPIC_API_KEY (gitignored)
├── .venv/                      # local Python 3.12 ARM64 venv (gitignored)
├── requirements.txt
├── CLAUDE.md                   # this file
├── README.md
├── summarize_council_meeting.py   # pipeline entry point
├── reprocess_published.py         # batch rebuild all published hearings
├── word_bank.json                 # persistent transcript corrections (reference data, not tracked)
├── summarize.log                  # gitignored
├── Input/                         # cached agenda PDFs and transcript JSONs (gitignored)
├── content/                       # hearing markdown files (YAML front matter + summary + transcript)
└── site/
    ├── build.py                   # Jinja2 + markdown → static HTML
    ├── templates/
    │   ├── base.html              # shared window chrome + MailerLite subscribe block
    │   ├── index.html             # listing page
    │   └── hearing.html           # single hearing page
    ├── static/site.css            # design system (Win95/XP palette, Inter + IBM Plex Mono)
    └── output/                    # built site, committed for Cloudflare Pages to serve
```

## Build and local preview

```bash
source .venv/Scripts/activate
python site/build.py
# then, from site/output/:
python -m http.server --bind 127.0.0.1 8001
```

Local preview **must** be via a served URL (`http://127.0.0.1:8001`), not `file://` — templates reference `/static/site.css` as an absolute path that only resolves under a served root.

## Summarizer pipeline

```bash
# Full run
python summarize_council_meeting.py <youtube_url> "Input/<agenda.pdf>" --title "Meeting Name"

# Use cached transcript JSON (no YouTube fetch)
python summarize_council_meeting.py --transcript-json "Input/<cached.json>" "Input/<agenda.pdf>"

# Common flags
--skip-fetch          reuse cached YouTube transcript
--skip-clean          bypass the cleanup step
--skip-summary SLUG   reuse existing page, update only transcript
--no-deploy           write markdown but skip build + git push (for batch mode)
--council-url <url>   Legistar MeetingDetail URL (renders as "View on council.nyc.gov")
```

Pipeline steps:
1. Fetch video metadata (title, duration) via yt-dlp
2. Fetch auto-generated YouTube transcript (youtube-transcript-api)
3. Extract agenda metadata via Claude (committee, date)
4. Segment transcript into speaker turns via Claude (index-based, not text/timestamp)
5. Clean transcript via Claude (style rules: expanded contractions, no Oxford commas, no comma splices, periods over semicolons, ellipses for pauses, `CM` for Councilmember, capitalised Council/City/Bill)
6. Format speaker headers (Chair/CM/witness conventions)
7. Remove oath and public testimony sections
8. Generate structured summary via Claude Sonnet
9. Write markdown with YAML front matter to `content/<slug>.md`
10. Run `site/build.py` to regenerate `site/output/`
11. `git add/commit/push` → Cloudflare Pages redeploys

Cost: ~$0.15–0.40 per meeting (Anthropic only, no external transcription).

### Caching

Raw YouTube segments, speaker turns, and cleaned utterances are all cached as a single `.json` per meeting in `Input/`. To re-run a specific stage:
- Delete `utterances` key → re-segment speakers
- Delete `cleaned_utterances` key → re-clean transcript
- Keep everything → pipeline skips straight to summary

### Config constants (top of `summarize_council_meeting.py`)
- `ANTHROPIC_MODEL`
- `MAX_TRANSCRIPT_CHARS` (summary chunking)
- `MAX_SEGMENT_CHARS` (segmentation batch)
- `MAX_CLEAN_CHARS` (cleanup batch)
- `SEGMENTATION_VERSION` (bump to invalidate speaker caches)

### Batch reprocess

`python reprocess_published.py` rebuilds every published hearing from its cached `raw_segments`, then does a single deploy at the end. Use after changing style rules or template logic.

## Deployment

- GitHub: `whiffythesheep/hearinghearings` (public repo)
- Host: Cloudflare Pages, build output dir = `site/output/`, no build command
- DNS: Cloudflare (`hearinghearings.nyc`)
- Trigger: any push to `master` auto-deploys

`site/output/` is **committed** to the repo (not gitignored) — this is how Cloudflare Pages serves the pre-built site without running a build step.

## MailerLite subscription

- Account: user's personal MailerLite account (seaneke@outlook.com)
- Free tier limit: 1,000 subscribers — migrate to Buttondown or ConvertKit before hitting this
- Embed form lives in `site/templates/base.html` inside the `{% block subscribe %}` block, so it renders on both the index and every hearing page
- Form styling is in `site/static/site.css` under `.subscribe-*` selectors to inherit the Win95 palette
- MailerLite account must have `hearinghearings.nyc` added as an allowed domain for embed submissions to succeed in production

## Inherited standards

- Excel naming (`YY.MM descriptive name`), formatting (Aptos Narrow 11, Notes/Raw tabs) from `~/.claude/CLAUDE.md` still apply for any spreadsheet outputs
- Style rules for transcripts documented in-line in the summarizer prompt; see memory `project_transcript_style_rules.md`
