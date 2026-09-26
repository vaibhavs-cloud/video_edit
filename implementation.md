# AI Video Editing Agent — Implementation Plan

**Status:** **complete** (M0–M5 built and verified 2026-09-26; live evidence in git history + Actions runs + Telegram deliveries).
**Companion doc:** `video-editing-agent-spec.md` (product spec — §14 corrections have been applied).
**Rule for anyone (human or agent) reading this:** every external API/limit below was verified against live docs on 2026-09-26. If something fails at runtime, check §12 (known runtime-only checks) before redesigning anything.

### Deviations from the original plan (justified)

1. **LLM provider switched to Groq** (`config.yaml models.provider: groq`: `whisper-large-v3` + `openai/gpt-oss-120b`) — Gemini's free tier turned out to be **20 requests/day** for `gemini-3.5-flash`, exhausted within one build day. Gemini stays fully supported behind the same `vedit/llm.py` abstraction (swap `models.provider`); all model IDs remain config knobs.
2. **Correction loop implemented as specified in §9** (structured `FixOp` patch, never re-plans) rather than an intermediate re-plan-with-instruction version used during early testing.
3. Icon pick "none" outcomes are spec-compliant (best-of-8-or-none); an *explicit user-requested* icon replacement falls back to Iconify's top candidate so a fix never silently renders nothing.

---

## 1. Locked decisions

| Topic | Decision |
|---|---|
| Runtime | GitHub Actions (**public repo** → free unlimited minutes), triggered by a Cloudflare Worker webhook |
| Dispatch mechanism | `workflow_dispatch` (max 25 inputs, official) — NOT `repository_dispatch` |
| Run state | GitHub artifacts — **small state only** (JSON/MD/SRT/PNG), never media |
| Artifact storage | Free plan = 500 MB total (shared with Packages) ⇒ state artifacts ~1 MB/run, `retention-days: 30` |
| Correction loop | In MVP (Telegram reply → patch `edit_plan.json` → re-render → re-send) |
| Source format | Auto-detect aspect → deterministic 9:16 reframe (center crop, `x_frac` override) |
| Visuals | Iconify icons (pinned sets `lucide,ph`) + user-attached screenshots |
| Primary input path | Google Drive share link (`gdown`); ≤20 MB Telegram attachment as fallback |
| Language / layout | Python 3.12, single package `vedit/`, CLI-driven; **zero business logic in YAML or Worker** |
| GitHub account | The **other** account (per `keys.txt` → `GH_OWNER`), repo created via `gh` CLI |
| Cloudflare account | **Vaibhav personal** (id `60d867f2eaa9a1dababb2d248d2768aa`) — persist in `opencode.json` + project `AGENTS.md` at M0 |
| Dispatch token | Classic PAT, scope **`repo` only** (documented for `workflow_dispatch`) |

## 2. Verified components (live-doc checks, 2026-09-26)

| # | Component | Verdict | Notes |
|---|---|---|---|
| 1 | `gemini-3.5-transcribe` word timestamps (`word_timestamp=True`) | ✅ | Degrades ASR accuracy slightly; **incompatible with custom vocabulary**; ≤30 min audio with timestamps |
| 2 | `gemini-3.5-flash` structured outputs (`response_format` + JSON Schema) | ✅ | Schema subset supports `enum`, `minItems/maxItems`, `additionalProperties:false` — all we use |
| 3 | Gemini free tier | ✅ | Per-model RPM/RPD visible in AI Studio. We use 3–4 requests/video. ⚠️ Keys must be restricted (AI Studio keys are auto-restricted since Jun 19 2026). ⚠️ Free tier = content may be used to improve Google products |
| 4 | Iconify search (`prefixes`, `limit`) | ✅ | `limit` min 32 / max 999, default 64; `prefixes=lucide,ph` pins style. No published hard rate limit (self-host advice only above 1k req/min). We do ≤10 req/video |
| 5 | Telegram `getFile` 20 MB download / send 50 MB | ✅ | Shapes input contract: link-first, attachment ≤20 MB |
| 6 | Telegram `setWebhook secret_token` → `X-Telegram-Bot-Api-Secret-Token` header | ✅ | 1–256 chars, `[A-Za-z0-9_-]` only |
| 7 | Cloudflare Workers Free | ✅ | 100,000 req/day, **10 ms CPU/req**, 50 subrequests/req, 64 env vars, 64 MiB worker, no card |
| 8 | GitHub Actions free on public repos | ✅ | "Usage is free … in public repositories" (docs billing page) |
| 9 | GitHub artifact storage | ✅ tight | Free plan **500 MB total, shared with Packages**; retention 1–90 days; ≤500 artifacts/run |
| 10 | `workflow_dispatch` REST | ✅ | Max **25 inputs**; classic PAT needs `repo` scope; **workflow file must be on default branch** |
| 11 | `ubuntu-latest` (24.04) runner includes ffmpeg | ❌ **corrected** | Read image readme directly: `file`/`flex`/`gcc` present, **no ffmpeg, no rsvg** → CI must `apt-get install ffmpeg librsvg2-bin` |
| 12 | FFmpeg filters in plan | ✅ | `highpass, afftdn, acompressor, loudnorm (2-pass), trim/atrim, setpts, afade, scale, crop, overlay, concat demuxer, ass+fontsdir, tile, movflags+faststart` — all standard; Ubuntu ffmpeg has libass |
| 13 | `svglib[bitmaps]` / `rlPyCairo` | ✅ | Local fallback only; `rsvg-convert` primary on CI |
| 14 | Cross-run artifact download | ✅ | `actions/download-artifact` + `run-id` + token with `actions: read` (explicit `permissions:` block) |
| 15 | Local machine | ✅ | ffmpeg 2026-02 full build, Python 3.12.10, git 2.52, Node 24, wrangler 4.141 via npx. `gh` ❌ → installed at M0 |

**Runtime-only checks (with fallbacks):** ① `gemini-3.5-transcribe` accepts free-tier key → fallback: word timestamps from `gemini-3.5-flash` structured call. ② SVG→PNG on Windows → fallback chain ends at `rsvg-convert`. ③ Telegram `file_id` re-download durability → fallback: bot asks for the link again.

## 3. Limits vs headroom

| Resource | Limit | Our usage | Headroom |
|---|---|---|---|
| Gemini free tier | per-model RPM/RPD (AI Studio) | 3–4 req/video | ~25+ videos/day at pessimistic quotas |
| Iconify | no published cap | ≤10 req/video | ∞ |
| Actions minutes | **unlimited (public)** | 4–6 min/video | ∞ |
| Actions artifact storage | **500 MB shared** | ~1 MB/run, 30-day retention | hundreds of runs |
| Cloudflare Worker | 100k req/day, 10 ms CPU | 4 req/video, ~1 ms CPU | ∞ |
| Telegram in/out | 20 MB download / 50 MB send | link-first input; output ≈ 15–40 MB | ✅ |
| Job runtime | 6 h max | `timeout-minutes: 20` | ✅ |
| Payment | — | — | **zero cost, no card anywhere** |

## 4. Prerequisites (keys.txt)

Fill `keys.txt` at the project root. **Never commit it** — first action of M0 is `.gitignore`-ing it (before any commit exists). Values only ever go to GitHub Secrets / Worker vars.

```txt
GEMINI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
GH_TOKEN=
GH_OWNER=
GH_REPO=
TELEGRAM_SECRET_PATH=
TELEGRAM_SECRET_TOKEN=
```

| Key | How to get | Verify |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com/apikey → Create API key (Cloud project creation is free, no card) | first live call in M1 |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` (username must end in `bot`) | open `https://api.telegram.org/bot<TOKEN>/getMe` |
| `TELEGRAM_CHAT_ID` | **send your bot a message first**, then open `.../bot<TOKEN>/getUpdates` → `"chat":{"id":…}` (or @userinfobot) | appears in `getUpdates` |
| `GH_TOKEN` | **logged in to the other account**: github.com/settings/tokens → *Generate new token (classic)* → expiration 90 days → scope **`repo` only** → `ghp_…` (if GitHub demands 2FA: enable it first, then retry) | `gh auth login` at M0 |
| `GH_OWNER` | username of that other account | — |
| `GH_REPO` | chosen repo name (e.g. `vedit`) | — |
| `TELEGRAM_SECRET_PATH` | generate: `[guid]::NewGuid().ToString('N')` in PowerShell (32 hex) | becomes webhook path |
| `TELEGRAM_SECRET_TOKEN` | same command, second output | `[A-Za-z0-9_-]`-safe |
| `ALLOWED_CHAT_ID` | = `TELEGRAM_CHAT_ID` (no separate action) | — |

**Machine state:** ffmpeg ✔ Python 3.12 ✔ git ✔ Node ✔ wrangler(npx) ✔. To install at M0: GitHub CLI (winget). Python deps installed by me at M0.

**Sample material for M1/M2 acceptance:** one vertical + one landscape raw recording (20–90 s; Drive link if >20 MB) and 1–2 PNG screenshots.

## 5. Repository layout

```
vedit/
  __init__.py
  cli.py              # entrypoint: process | fix | qc | report  (staged + resumable)
  config.py           # config.yaml → frozen dataclass (every tuning knob lives here)
  schema.py           # pydantic: EditPlan, Word, Segment, Visual, CaptionSpan, StateMeta, FixOp
  stage/
    audio.py          # extract, enhance (2-pass loudnorm), 16 kHz mono for STT
    transcribe.py     # Gemini call 1 → transcript.json (words with offsets)
    cuts.py           # word-gap rules → segments (pure functions, no I/O)
    timeline.py       # TimelineMap: to_output(t) / to_source(t)
    plan.py           # Gemini call 2 → EditPlan (word indices) + validate + 1 repair retry
    visuals.py        # Iconify shortlist → Gemini call 3 (pick-or-none); screenshot assignment
    assets.py         # render_svg_to_png(), icon cache, screenshot prep
    render.py         # base reframe, per-segment encode, concat, ASS burn
    captions.py       # words + emphasis → .srt + .ass (output-timeline; no group spans a cut)
    qc.py             # gates → qc.json (pass/fail + reasons)
    report.py         # edit_report.md + contact_sheet.jpg + preview.mp4 (540x960)
  telegram_client.py  # getFile download, sendVideo/sendDocument/sendPhoto
  state.py            # save/load state artifact dir, sha8 state_refs
assets/fonts/         # Inter-SemiBold.ttf bundled (deterministic captions, OFL license)
assets/reference/     # optional screenshots for local runs
tests/
  unit/               # cuts, timeline, schema, captions, svg smoke
  fixtures/           # recorded transcript.json + EditPlan JSON (mocked Gemini)
  golden/sample.mp4   # ~8 s, 720x1280, ~1 MB (public repo — keep it tiny)
.github/workflows/edit.yml     # the pipeline (workflow_dispatch)
.github/workflows/test.yml     # pytest on push/PR, no API keys needed
infra/worker.mjs               # Telegram webhook → workflow_dispatch
infra/wrangler.toml
infra/set_webhook.sh
config.yaml                    # thresholds, model ids, styles, encoder presets
keys.txt                       # local only, gitignored from the first commit
implementation.md              # this file
```

## 6. Data contracts

### 6.1 `edit_plan.json` — all source-time, **word indices** (never float cut points)

```jsonc
{
  "version": 1,
  "source": {"ref": "a1b2c3d4", "sha256": "...", "w": 1920, "h": 1080, "dur": 71.4},
  "reframe": {"mode": "crop", "aspect": 0.5625, "x_frac": 0.5},
  "words": [{"i":0,"t":"An","s":0.12,"e":0.28}],
  "segments": [{"keep_from_word":0,"keep_to_word":41}],   // gaps between entries = cuts
  "visuals": [
    {"id":"v1","kind":"icon","keyword":"API","icon":"lucide:arrow-left-right",
     "from_word":18,"to_word":63,"pos":"top-right","zoom":false},
    {"id":"v2","kind":"screenshot","file_id":"AgACAgQ...","file":"diagram.png",
     "from_word":120,"to_word":170,"pos":"top","zoom":false}
  ],
  "captions": [{"from_word":0,"to_word":41,"emphasis":[18,19]}],
  "prompt": "keep it under 60s"
}
```

Validation (pydantic): word indices in range · `from < to` · no caption group spanning a cut · ≤ `max_visuals_per_min` · every asset resolvable-or-skippable.
**Failure path:** one LLM repair retry (validation errors injected into the prompt) → else degrade to captions-only plan. The pipeline never dies.

**Why word indices:** immune to timeline drift, trivially validatable, and every user correction ("icon at 0:20") maps to exactly one entry.

### 6.2 State artifact `state-<sha8>.zip` (uploaded on every successful run, 30-day retention)

```
meta.json        # source ref (telegram file_id | drive url), chat_id, config_hash, created_at
edit_plan.json   # prior plan — basis for fix patches
transcript.json
captions.srt  captions.ass
qc.json  edit_report.md
icons/*.png      # rendered SVGs (small)
```

Media (raw/base/segments) is **never stored** — re-derived per run (Drive URL / Telegram `file_id` both re-fetchable; base+segments rebuild in ~1 min). This is forced by the 500 MB artifact quota and keeps state ≈1 MB.

### 6.3 Dispatch payloads (`workflow_dispatch` inputs, all strings)

```jsonc
{
  "kind": "new", "chat_id": "123", "url": "<drive link>", "video_file_id": "",
  "attachments": "[\"file_id1\",\"file_id2\"]", "prompt": "keep it under 60s",
  "state_ref": "", "instruction": ""
}
```
`kind=new` uses `url` XOR `video_file_id`; `kind=fix` uses `state_ref` + `instruction`. Max 25 inputs (we use 8). The same inputs serve manual *Run workflow* from the Actions tab (debugging).

## 7. Pipeline stages

### A. Input acquisition
- `kind=new, url` → `gdown` (Drive) or `curl -L` (direct URL).
- `kind=new, video_file_id` → `telegram_client.download()`; **fails loudly if >20 MB** → bot replies asking for a link.
- Attachments → download each, preserve filenames.
- `sha256(source)` → `state_ref` (first 8 hex chars).

### B. Audio (`stage/audio.py`) — deterministic
```bash
# B1 extract (also feeds STT) — keeps source timeline
ffmpeg -i in.mp4 -vn -ac 1 -ar 16000 work/audio_raw.wav
# B2 measure pass
ffmpeg -i audio_raw.wav -af "highpass=f=80,afftdn=nf=-30,acompressor=threshold=-18dB:ratio=3:attack=10:release=200,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json" -f null -
# B3 apply pass with parsed measured_* → work/audio_clean.wav
```
All filters length-preserving ⇒ `audio_clean.wav` stays aligned to the source timeline and is **the audio source for every segment** (cut with identical `atrim` bounds as the video).

### C. Transcribe (`stage/transcribe.py`) — Gemini call 1
`gemini-3.5-transcribe`, `AudioTranscriptionConfig(word_timestamp=True)`, input = 16 kHz mono wav (≈2 MB/min → inline OK to ~7 min; longer → Files API). Output normalized to `transcript.json: [{i,t,s,e}]`. Retry ×2 with backoff. QC later enforces word coverage ≥95%.

### D. Cuts (`stage/cuts.py`) — pure, deterministic, unit-tested
```
gap = words[i+1].s - words[i].e
gap >= cut_gap_ms (600)   → cut between i / i+1, extended by pad_ms (120) each side,
                             clamped to never touch a word's [s,e]
gap <= keep_gap_ms (350)  → always keep
350 < gap < 600           → keep if < rolling-median-gap × 1.8 (speaking-rate adaptive)
min_kept_segment_ms (700) → merge away micro-segments
if output would fall below min_output_s (40): relax threshold once + log warning
```
LLM never emits a cut timestamp. Output: `segments[]` in source time.

### E. Plan (`stage/plan.py`) — Gemini call 2
`gemini-3.5-flash` + `response_format: {type:"text", mime_type:"application/json", schema: EditPlan.model_json_schema()}`.
Input: transcript with inline word indices (`[18]An API is…`) · user prompt · screenshot inventory · target duration.
Output: visuals/captions/emphasis/zoom flags only — **never cut times** (Stage D owns those; the plan embeds its result).
On schema failure: 1 repair retry with errors injected → else captions-only degrade.

### F. Visuals (`stage/visuals.py`) — Gemini call 3 (tiny, batched)
1. Per icon keyword: `GET https://api.iconify.design/search?query=<kw>&prefixes=lucide,ph&limit=32` → top 8 ids.
2. **One** structured `gemini-3.5-flash` call receives *all* shortlists → `{visual_id: icon_id | null}` (null = drop the visual). Deterministic post-checks: density cap ≤1 per 8 s, total ≤ `max_visuals`.
3. Screenshots: plan already assigned `file_id` ranges — no extra call.
4. Cache `icons/<prefix>-<name>-<color>.png` in workdir + state artifact (reproducible renders; neutralizes upstream icon churn).
5. `render_svg_to_png()` seam: `rsvg-convert` (CI) → `svglib[bitmaps]` (local) → raise with instructions. One smoke test covers it.

### G. Render (`stage/render.py`)
1. **Reframe** (once, cached by source sha): already 9:16 → `scale=1080:1920`; else
   `crop=ih*9/16:ih:x=(iw-ih*9/16)*x_frac,scale=1080:1920` → `work/base.mp4` (video-only). `x_frac` default 0.5, `--crop-x` override for off-center takes.
2. **Per segment** (parallelizable `xargs -P 2`), frame-accurate, video+audio identically trimmed:
```bash
ffmpeg -i base.mp4 -i audio_clean.wav -i overlay.png -filter_complex "\
 [0:v]trim=start=S:end=E,setpts=PTS-STARTPTS[v0];\
 [v0]scale=iw*Z:-2,crop=1080:1920[v1];\
 [2:v]format=rgba,fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st=DUR-0.25:d=0.25:alpha=1[ov];\
 [v1][ov]overlay=x=X:y=Y:enable='between(t,VS,VE)'[v2];\
 [1:a]atrim=start=S:end=E,asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st=DUR-0.01:d=0.01[a]" \
 -map "[v2]" -map "[a]" -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -r 30 \
 -c:a aac -b:a 192k -ar 48000 seg_<n>.mp4
```
   - `Z` ∈ {1.0, `zoom_factor`=1.06} per plan's zoom flag; screenshots scale to 90 % width, top-centered, 6 px border.
   - Overlay clipped to segment; fade-in only if the visual *starts* inside this segment, fade-out only if it *ends* there (a visual spanning a cut must not double-fade).
   - **Identical encoder settings across all segments** (hard requirement for stream-copy concat).
3. **Concat:** `ffmpeg -f concat -safe 0 -i list.txt -c copy work/concat.mp4` (lossless, near-instant).
4. **Caption burn** (single final pass, OUTPUT timeline — where `TimelineMap` applies):
```bash
ffmpeg -i work/concat.mp4 -vf "ass=work/captions.ass:fontsdir=assets/fonts" \
  -c:a copy -movflags +faststart final_video.mp4
```
   Chosen over per-segment burn: one ASS file, no timestamp rebasing, no flicker at cut boundaries, and a caption-only fix costs one encode of a ~60 s file while `concat.mp4` stays byte-untouched.

### H. QC gate (`stage/qc.py`) — hard-fail blocks delivery
| Check | Threshold |
|---|---|
| output duration vs `source − cuts` | ±0.25 s |
| ffprobe: streams present, non-empty, final has video+audio | pass |
| captioned words / transcript words | ≥95 % |
| every visual's icon/screenshot resolves | else drop visual + report it |
| overlays within frame bounds (computed) | pass |
| 1080×1920 @ 30 fps | pass |
| output ≤ max_output_s (90) | warn only |
| any LLM call degraded/failed | warn only, flagged in report |

Failure ⇒ send `edit_report.md` + reasons to Telegram (**no video**), exit non-zero.

### I. Report + deliver (`stage/report.py`, `telegram_client.py`)
- `edit_report.md`: time removed (s + %), cut count, visual list w/ timestamps, caption count, QC results, degraded flags, `state_ref` + supported fix phrasings.
- `contact_sheet.jpg` (2×3 `tile` filter) + `preview.mp4` (540×960, crf 30) for cheap phone review.
- Deliver: `sendVideo` (final, caption = summary + `state_ref`) → `sendPhoto` (contact sheet) → `sendDocument` (report). If final >50 MB → `sendDocument` fallback.

## 8. Infra

### 8.1 Cloudflare Worker (`infra/worker.mjs`) — Vaibhav personal account
```
POST /tg/<TELEGRAM_SECRET_PATH>   (+ header X-Telegram-Bot-Api-Secret-Token == TELEGRAM_SECRET_TOKEN)
 ├─ chat_id != ALLOWED_CHAT_ID            → 200 ignore (public surface: never trust senders)
 ├─ text contains URL                     → dispatch kind:new (url)
 ├─ video attachment ≤20 MB               → dispatch kind:new (video_file_id)
 ├─ video attachment >20 MB               → reply "too big — send a Drive link"
 ├─ text replies to a bot msg w/ state_ref→ dispatch kind:fix (state_ref + instruction)
 └─ GET /health                           → 200
```
Env: `TELEGRAM_BOT_TOKEN, TELEGRAM_SECRET_PATH, TELEGRAM_SECRET_TOKEN, ALLOWED_CHAT_ID, GH_OWNER, GH_REPO, GH_TOKEN`.
Deploy: `npx wrangler deploy` → `infra/set_webhook.sh` calls `setWebhook` (url + `secret_token`) once.
Dispatch call: `POST /repos/{GH_OWNER}/{GH_REPO}/actions/workflows/edit.yml/dispatches` with `ref: main`, classic PAT (scope `repo`), `inputs` per §6.3.

### 8.2 `.github/workflows/edit.yml`
```yaml
on:
  workflow_dispatch:            # Worker + manual "Run workflow" share these inputs
    inputs:
      kind:        { type: string,  required: true }   # new | fix
      chat_id:     { type: string,  required: true }
      url:         { type: string }
      video_file_id: { type: string }
      attachments: { type: string }   # JSON array string
      prompt:      { type: string }
      state_ref:   { type: string }
      instruction: { type: string }
concurrency: { group: vedit-${{ inputs.chat_id || 'manual' }}, cancel-in-progress: false }
permissions: { contents: read, actions: read }
timeout-minutes: 20
runs-on: ubuntu-latest
steps:
  - checkout
  - setup-python@v5 (3.12, cache: pip)
  - sudo apt-get update && sudo apt-get install -y ffmpeg librsvg2-bin   # NOT preinstalled (verified)
  - assert: ffmpeg -filters | grep -q ass
  - pip install -r requirements.txt
  - vedit process … | vedit fix …        # all logic lives here
  - upload artifact state-<sha8> (retention-days: 30, only if QC passed)
  - deliver via Telegram
```
- **Constraint:** workflow file must be on `main` before the Worker's first trigger (M3 ordering).
- Cross-run state download: `actions/download-artifact@v4` with `run-id` + `github-token: ${{ secrets.GITHUB_TOKEN }}` (needs explicit `actions: read`).
- `test.yml`: `pytest` on every push/PR using `--mock` fixtures (no secrets, no network).

## 9. Correction loop (`kind=fix`)

1. Owner replies to the delivered video: *"fix the icon at 0:20, use a shield"*.
2. Worker → `workflow_dispatch` (`kind=fix`, `state_ref`, `instruction`).
3. Job downloads `state-<sha8>`, re-derives source from `meta.json` (Drive URL / `file_id`).
4. **Gemini call (small, structured)** maps free text → patch op enum:
   `replace_icon | remove_visual | recaption | retime_visual | unknown`
   (`unknown` → bot replies with supported phrasings, **no render**).
5. Patch `edit_plan.json` (re-validated), re-run stages G–I. Transcription & plan untouched.
   Segments are re-rendered in full (deterministic; unchanged segments come out identical) — cross-run byte-reuse is a documented future optimization, not an MVP dependency.
6. New video + short report sent; new state uploaded under the **same `state_ref`** (overwrites) so corrections can chain.

Adding a new op = one schema entry + one patch function + one test.

## 10. Testing strategy

- **Unit (pure, no I/O):** cut rules across gap tables · `TimelineMap` round-trips · caption groups never cross cuts · plan validation rejects out-of-range indices · SVG smoke (`lucide:shield` → non-empty PNG).
- **Golden run:** `vedit process tests/golden/sample.mp4 --mock` (recorded fixtures) → plan snapshot + render completes + QC passes. Runs in `test.yml` on ubuntu ⇒ render regressions caught on every push.
- **Live smoke (manual, Run-workflow):** one real Drive URL through the whole chain before each milestone is called done.
- **Pinning:** `requirements.txt` pins all deps; model IDs live in `config.yaml`, never in code.

## 11. Milestones (strict order, each gated by acceptance)

| # | Scope | Acceptance | Est. |
|---|---|---|---|
| **M0** | `.gitignore` (keys.txt first!) · `implementation.md` ✅ · gh install (winget) + `gh auth login` (other account) · create public repo · set 3 GitHub secrets · scaffold package + `schema.py` + `config.yaml` + `test.yml` · persist Cloudflare choice (`opencode.json` pin + AGENTS.md line) | `pytest` green in CI on the new repo | 0.5 d |
| **M1** | Stages B–D + captions + minimal render (no visuals); `vedit process --mock` and live locally | watchable locally-edited video from a real recording: silence gone, clean audio, burned captions | 1.5 d |
| **M2** | Stages E–F (plan + icons + screenshots) + QC gate + report/contact sheet/preview | full local run on a real video; QC catches a deliberately broken plan | 1.5 d |
| **M3** | Worker (Vaibhav personal) + `edit.yml` on main + Telegram in/out (Drive primary, attachment fallback) + `set_webhook.sh` | phone → bot → finished video in ≤5 min, zero local commands | 1 d |
| **M4** | Fix dispatch, state artifacts, patch parser, chained re-render | *"fix the icon at 0:20"* → new video ≤3 min, no re-transcription | 1 d |
| **M5** | Polish: preview+contact sheet delivery, README setup checklist, error messages, optional segment parallelism | cold setup reproducible from README on a clean machine | 0.5 d |

**Total ≈ 6 focused days.** M1/M2 verified locally; M3/M4 verified via live workflow runs.

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Telegram 20 MB download cap (hit on most phone recordings) | Link input is the documented **primary** path; bot auto-requests a link when over cap |
| 500 MB artifact quota (shared with Packages) | State-only artifacts (~1 MB), media re-derived, `retention-days: 30`, optional prune via `DELETE /…/actions/artifacts/{id}` |
| Public repo ⇒ strangers trying to trigger runs; artifacts semi-public | Worker `ALLOWED_CHAT_ID` allowlist · never store media in artifacts · secrets only in GH Secrets/Worker vars |
| Gemini model renames (IDs already changed once in 2026) | Model IDs in `config.yaml`; QC marks degraded runs; 1 repair retry |
| Word timestamps reduce ASR accuracy | QC coverage ≥95 % gate; `vedit process --from transcribe` re-run path |
| Iconify no SLA / upstream icon changes | Content-addressed PNG cache in state; missing icon → drop + report, never crash |
| SVG→PNG platform quirks | Single seam + fallback chain + CI smoke test |
| `gemini-3.5-transcribe` unavailable on free tier | Fallback: word timestamps from `gemini-3.5-flash` structured call (§2) |
| Workflow not on `main` yet when Worker fires | M3 ordering: push `edit.yml` to main **before** `setWebhook` |
| Actions silent failure | QC hard-fail + non-zero exit + Telegram error message + report always sent |
| keys.txt leaked into git | `.gitignore` is the very first commit-scoped action (M0), before anything else exists |

## 13. Secrets inventory (where each value lives)

| Value | GitHub repo secrets | Worker env | keys.txt |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | — | ✅ |
| `GROQ_API_KEY` | ✅ | — | ✅ |
| `TELEGRAM_BOT_TOKEN` | ✅ | ✅ | ✅ |
| `TELEGRAM_CHAT_ID` | ✅ (= `ALLOWED_CHAT_ID`) | ✅ | ✅ |
| `GH_TOKEN` (classic, scope `repo`) | — | ✅ | ✅ |
| `GH_OWNER` / `GH_REPO` | — | ✅ | ✅ |
| `TELEGRAM_SECRET_PATH` | — | ✅ | ✅ |
| `TELEGRAM_SECRET_TOKEN` | — | ✅ | ✅ |

## 14. Spec corrections to apply to `video-editing-agent-spec.md` (at M0, minimal edits)

> **APPLIED 2026-09-26** — all four corrections are now in the spec (§3 input/reframe, §4 architecture/silence, §5 state/render, §6 visuals), plus honest provider/free-tier updates (§2 principle 3, §8 table) reflecting the Groq switch.

1. **§3 Input contract:** Drive-link input is primary (Telegram `getFile` ≤20 MB hard cap); attachment fallback; auto 9:16 reframe rule (`x_frac` override).
2. **§4 Architecture:** silence detection leaves Gemini → word timestamps + deterministic rules; transcribe/plan split into two schema-validated calls (`gemini-3.5-transcribe` + `gemini-3.5-flash`); dispatch is `workflow_dispatch`; CI installs ffmpeg.
3. **§5 Rendering:** captions burned in one final pass over the concat (not per-segment); cross-run state = small artifact, media re-derived (replaces the byte-reuse claim; partial *decision* — no re-transcribe/re-plan — preserved).
4. **§6 Visuals:** `prefixes=lucide,ph` pinning + best-of-8-or-none pick + density cap + screenshot placement.

## 15. Non-goals (unchanged)

No AI-generated diagrams/thumbnails/titles · no multi-format output · no learning-from-corrections · no timeline preview UI · no auto-publishing · no second AI provider · no FastAPI/React · no VPS.
