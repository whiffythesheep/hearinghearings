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

## Full-text transcript search

The index-page search box queries a prebuilt inverted index that `build_search_index()` in `site/build.py` writes to `site/output/search/`: `docs.json` (slugs sorted date ascending + slug tiebreak — **do not change the ordering**; it keeps doc indices stable so adding a hearing only appends) and `idx-<a-z|0>.json` shards mapping token → `[[docIdx, count]]`. The client JS in `site/templates/index.html` lazy-loads only the shards a query needs, shows exact mention counts, and fetches matching `transcript.txt` files to render a highlighted excerpt (with exact-phrase verification for multi-word queries). Search-as-you-type: the index lookup runs on every keystroke (no debounce); the final token matches any indexed token it prefixes ("jul" → "julie", "july") unless the query ends with a space/punctuation, which makes it exact; earlier tokens are always exact. Excerpt fetches sit behind a 150ms debounce.

Two invariants shared between Python and JS — keep them in sync if either changes:
- **Tokenizer**: `\$?\d[a-z0-9']*(?:\.\d[a-z0-9']*)*|[a-z0-9']+` (`SEARCH_TOKEN_RE` in build.py, `TOKEN_RE` in index.html). Decimal/dollar aware: `.` continues a token only when a digit follows ("12.7" is one token; "12." and "e.g." split), `$` attaches only when a digit follows ("$3.5"). Dollar tokens are indexed under both forms ("$12.7" also counts toward "12.7") so bare-number queries surface dollar figures while `$`-prefixed queries match only dollar amounts. `$`- and digit-initial tokens share the `0` shard on both sides. Query tokens are always regex-escaped (`re.escape` / `escapeRegExp`) before interpolation.
- **transcript.txt header strip**: the files are CRLF; the JS separator regex must stay CRLF-tolerant (`\r?\n={16,}\r?\n`).

Rotating search-bar examples come from `SEARCH_EXAMPLE_CANDIDATES` in `build.py`, filtered at build time to phrases that actually occur in a published transcript.

## Summarizer pipeline

```bash
# Full run (YouTube)
python summarize_council_meeting.py <youtube_url> "Input/<agenda.pdf>" --title "Meeting Name"

# Viebit run (for hearings not uploaded to YouTube)
python summarize_council_meeting.py --viebit-url "<https://councilnyc.viebit.com/watch?hash=...>" "Input/<agenda.pdf>" --title "Meeting Name"

# Use cached transcript JSON (no fetch)
python summarize_council_meeting.py --transcript-json "Input/<cached.json>" "Input/<agenda.pdf>"

# Common flags
--viebit-url <url>    Viebit watch URL; use when the hearing isn't on YouTube. Requires --title.
--skip-fetch          reuse cached transcript
--skip-clean          bypass the cleanup step
--skip-summary SLUG   reuse existing page, update only transcript
--no-deploy           write markdown but skip build + git push (for batch mode)
--council-url <url>   Legistar MeetingDetail URL (renders as "View on council.nyc.gov")
```

Pipeline steps:
1. Fetch video metadata (title, duration) via yt-dlp (or skip when `--viebit-url`)
2. Fetch transcript — auto-generated YouTube captions (youtube-transcript-api) OR Viebit WebVTT sidecar (see "Viebit caption fetch" below)
3. Extract agenda metadata via Claude (committee, date)
4. Segment transcript into speaker turns via Claude (index-based, not text/timestamp)
5. Clean transcript via Claude (style rules: expanded contractions, no Oxford commas, no comma splices, periods over semicolons, ellipses for pauses, `CM` for Councilmember, capitalised Council/City/Bill)
6. Format speaker headers (Chair/CM/witness conventions)
7. Remove oath and public testimony sections
8. Generate structured summary via Claude Sonnet
9. Write markdown with YAML front matter to `content/<slug>.md` (fields: `committee`, `committee_slug`, `title`, `date`, `slug`, `duration`, `youtube_url`, optional `viebit_url`, optional `council_url`)
10. Run `site/build.py` to regenerate `site/output/`
11. `git add/commit/push` → Cloudflare Pages redeploys

### Viebit caption fetch

`fetch_viebit_transcript()` covers hearings that don't get uploaded to YouTube. It GETs the watch page, regexes the WebVTT URL out of the embedded player config (`"src": "https://vbfast-vod.viebit.com/counciln/<hash>/<asset>.vtt"`), fetches the VTT (no auth), and parses cues. Viebit captions are CEA-608-style ALL-CAPS rolling captions with heavy dual-position duplication, so the parser keeps a sliding window of the last 6 normalized lines and drops repeats — collapsing ~24K raw cues to ~6K clean segments for a 3.5-hour hearing. Output shape matches YouTube exactly (`text`/`start_ms`/`end_ms`), so everything downstream is source-agnostic. Duration is derived from the last segment's `end_ms`. Per-utterance timestamps render as plain `(HH:MM:SS)` in the transcript (no Viebit deep-link param is known).

Cost: ~$0.15–0.40 per meeting (Anthropic only, no external transcription).

### Caching

Raw YouTube segments, speaker turns, and cleaned utterances are all cached as a single `.json` per meeting in `Input/`. To re-run a specific stage:
- Delete `utterances` key → re-segment speakers
- Delete `cleaned_utterances` key → re-clean transcript
- Keep everything → pipeline skips straight to summary

### Council roster + name validation

`council_roster.json` (repo root, **tracked**) is the definitive list of who currently sits on the Council: all 51 districts, plus every committee/subcommittee with its chair and membership. Refresh it whenever membership changes:

```bash
python refresh_council_roster.py    # scrapes council.nyc.gov, ~40 requests, ~30s
```

It prints a `CHANGED District N: old -> new` line for any seat that moved, so special elections show up in the diff. Because the file is tracked, a membership change is visible in the PR.

**Why it exists.** The Claude cleaning pass (step 5) does not just fix garbled captions — when a surname is badly mangled it substitutes a *plausible but wrong* member, including members who have left office, and the summary then propagates that into Action Points as fact. Real case (2026-07-21, event 1416200): raw Viebit caption `COUNCIL MEMBER OF WRESTLERS` → cleaned to "CM Bottcher's district", when the Chair had said **Restler** and Bottcher had left for the State Senate five months earlier.

`validate_member_names()` in `summarize_council_meeting.py` runs inside `publish_to_website()` on every run (including `--no-deploy`, so the nightly discover flow gets it). It scans the finished markdown for title-prefixed names — `CM X`, `Chair X`, `Council Members A, B and C`, `Speaker X` — and logs a WARNING for any not on the roster.

**The roster also feeds chair lookup.** `build_committee_chair_lookup()` resolves co-committee chairs on joint hearings (agendas only name the *lead* committee's chair) from three layers, lowest precedence first:

1. `council_roster.json` — every committee and subcommittee, broad coverage.
2. This archive's own `content/*.md` front matter — agenda-derived, so it preserves the name forms used elsewhere on the site (middle initials: "Rita C. Joseph", not the roster's "Rita Joseph"). Wins over the roster for that reason.
3. `committee_chairs_supplement.json` — manual overrides, highest precedence.

Keys are matched through `normalize_committee_name()` (folds case, spells out `&`, drops punctuation), because agendas write "Committee on Oversight **&** Investigations" while council.nyc.gov writes "**and**" — an exact-match lookup silently missed it.

Since layer 1 landed (2026-07-25) the supplement is a **pure override file and is empty**. Its five entries were verified redundant — removing them changed no lookup result — and adding the roster layer filled in 6 previously-blank co-committee chairs across published hearings. Only add to it to override what the roster and archive already produce.

**`chairs` is positional** — slot *i* corresponds to committee *i* in the pipe-delimited `committee` field. A chair in the wrong slot is a real bug; the roster makes it detectable (one was found and fixed on 2026-07-25, where Finance's chair sat in the Consumer and Worker Protection slot).

The name validator is **advisory, never blocking**: witnesses, agency staff, state officials and genuinely former members all legitimately appear with a title. Expect ~1–2 flags per hearing. When one fires, check the name against the **district numbers printed in the agenda PDF** (authoritative — the transcript is not), and confirm what was actually said in `Input/<meeting>.json` → `raw_segments`. Then add a correction to `word_bank.json`, keeping it **prefixed** (`"Speaker Menon"`, not bare `"Menon"`) — bare surnames over-match, and "phenomenon" contains "menon".

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

### Email step (currently manual)

As of 2026-05-07 the pipeline does **not** send subscriber emails by default. MailerLite's free-tier API rejects custom HTML campaigns (422 since 2026-05-06), and a Buttondown migration was investigated but rejected — Buttondown's free tier wraps custom HTML in template chrome that can't be removed without paid tier. Subscriber notifications are now sent manually via a weekly digest the user composes in the MailerLite dashboard.

The email-rendering and -sending code is preserved for when the user upgrades or migrates:

Flags:
- Default (no flag) — pipeline skips the email step entirely.
- `--send-email` — attempt to send via MailerLite API (will 422 on free tier until upgrade).
- `--skip-preview` — when used with `--send-email`, broadcasts directly to all subscribers without preview prompt.

Preview group setup (still required if/when `--send-email` is used):
1. In MailerLite → Subscribers → Groups → create a group named `Preview`.
2. Add `seaneke@outlook.com` (or whichever address is the preview recipient) as the only subscriber in that group.
3. Copy the group ID and add to `.env`:
   ```
   MAILERLITE_PREVIEW_GROUP_ID=<id>
   ```

### Weekly digest (`weekly_digest.py`)

`weekly_digest.py` assembles a ready-to-send **weekly** digest email — the replacement for the old per-hearing automated send. It is standalone (no API key) and decoupled from the pipeline: it reuses the pure helpers in `site/build.py` (`parse_front_matter`, `split_summary_transcript`, `truncate_text`, `SITE_URL`, `CONTENT_DIR`) and mirrors the Win95 chrome of `_render_email_html()`.

```bash
python weekly_digest.py                                   # trailing 7 days
python weekly_digest.py --since 2026-06-15 --until 2026-06-21
python weekly_digest.py --sentences 3 --output digest.html
```

It collects every hearing in the date window (default: trailing 7 days, overridable with `--since`/`--until`), pulls the **first 2-3 sentences of each Meeting Overview verbatim** as a brief (sentence splitter guards against ellipsis/abbreviation false breaks), and writes a self-contained HTML file to `weekly_digests/weekly-digest-<until>.html` (gitignored). It prints the matched hearings and a suggested subject line. The HTML has a clearly marked `<!-- COMMENTARY -->` block for the hand-written opening; the user fills that in, sets the subject, and sends.

**Send route:** MailerLite's **Custom HTML editor is gated to paid plans until 2026-07-01**, when the user's free account transitions and gains it (Create Campaign → Custom HTML editor → paste). The script's output is built for that editor. **Caveat:** the same 2026-07-01 free-plan update drops limits to **250 active subscribers / 2,500 emails per month** (the "1,000 subscribers" figure above is now stale) — migrate ESPs before the list approaches 250.

## Inherited standards

- Excel naming (`YY.MM descriptive name`), formatting (Aptos Narrow 11, Notes/Raw tabs) from `~/.claude/CLAUDE.md` still apply for any spreadsheet outputs
- Style rules for transcripts documented in-line in the summarizer prompt; see memory `project_transcript_style_rules.md`
