# AI Video Editing Agent — Project Spec

**Owner:** Vaibhav
**Purpose of this doc:** Full context for any agent (coding agent, LLM, or human contributor) picking up this project. Read this before writing any code.

---

## 1. What this is

A personal, zero-touch AI agent that turns one raw talking-head recording into one publish-ready vertical Reel, with almost no manual editing time from the owner.

The owner records himself explaining a software/CS concept (APIs, JWT, authentication, etc.) for an educational Reels series. He does not want to spend time editing, searching for visuals, or building any asset library. He wants to review a finished video and either publish it or request a targeted fix — not re-edit it himself.

**Non-goal:** This is not a general-purpose autonomous video editor. It is not trying to replace human judgment on final quality — only the repetitive mechanical editing work.

---

## 2. Guiding principles (do not violate these when building)

1. **AI decides *what*, deterministic rules decide *how*.** The LLM makes editing decisions (what to cut, what concept was mentioned, what visual fits). Rendering itself is handled by fixed, predictable FFmpeg logic — never by asking an LLM to "generate a video."
2. **Zero upfront manual work.** No pre-built visual asset library, no manual scripts, no timestamps, no manually selected clips. Everything must be inferred from the raw recording at run time.
3. **Single LLM provider per run.** Do not split reasoning and transcription across multiple AI providers *within one pipeline*. One provider handles all three calls (transcription, plan, icon pick) to avoid multi-provider fragility. The provider is a `config.yaml` knob, not code: current choice **Groq** (whisper-large-v3 + gpt-oss-120b) because Gemini's free tier proved too small for the build (20 requests/day); Gemini remains a supported fallback provider behind the same abstraction.
4. **Fully free infrastructure, no credit card, always available on demand.** No persistent VPS. The system should be idle-cost-zero and only consume compute when actually processing a video.
5. **Output format: Reels/Shorts.** Vertical 1080×1920. Style should be "raw," not over-produced — minimal, native-feeling overlays, not motion-graphics-heavy explainer style.
6. **Partial re-editing must be possible.** The owner must be able to fix one part of a video (one visual, one caption) without a full re-render of the whole thing. This is a hard structural requirement, not a nice-to-have — design the rendering pipeline around it from day one.
7. **Don't over-engineer.** No UI beyond Telegram. No learning-from-corrections system, no preset system, no multi-format output — not yet. MVP first.

---

## 3. Input / Output contract

### Input
| Type | Required? | Example |
|---|---|---|
| Raw video (MP4/MOV), talking-head, clear speech | Required | `api_explanation.mp4` |
| Free-text instructions (topic, target duration, emphasis) | Optional | "Explain APIs, keep it under 60 seconds" |
| Reference assets (screenshots, logos, screen recordings) | Optional | `diagram.png` |

**Delivery of the raw video:** the Telegram Bot API caps file *downloads* at 20 MB, so a Google Drive share link (opened with `gdown`) is the **primary** input path; a ≤20 MB Telegram attachment is the fallback. The agent auto-detects the source aspect ratio and reframes deterministically to 9:16 (center crop, `x_frac` override in config).

The agent must never require a script, manual timestamps, a shot list, or a pre-selected image list. It infers all of that itself.

### Output
| File | Purpose |
|---|---|
| `final_video.mp4` | Primary deliverable — 1080×1920, ready to review and publish |
| `captions.srt` | Timestamped subtitles, reusable/correctable independently |
| `edit_plan.json` | Machine-readable timeline of every decision made (cuts, visuals, captions) — this is what makes targeted re-edits possible |
| `edit_report.md` | Human-readable summary: what was cut, what was inserted, what to double check |

### Expected quality bar (60s explainer example)
| Element | Expectation |
|---|---|
| Silence | Long pauses removed; feels like natural jump cuts, not aggressive chopping |
| Audio | Noise-reduced, normalized, consistent volume |
| Captions | Accurate, synced, readable |
| Visuals | Concept-relevant icon/diagram appears at the right moment, disappears when done |
| Transitions | Clean cuts, occasional subtle zoom — restrained, never "meme-edited" |
| Format | 1080×1920 vertical |

---

## 4. Architecture (free, always-on-demand, no VPS)

```
Telegram (owner sends raw video / instructions, receives final video)
        │
        ▼
Cloudflare Worker  — always-on webhook receiver, free tier (100k req/day, no card)
        │  (dispatches workflow_dispatch with typed inputs)
        ▼
GitHub Actions  — burst compute, free tier (unlimited minutes on a public repo)
        │  (installs ffmpeg + librsvg on the runner; asserts the ASS filter)
        │
        ├─► LLM provider (schema-validated calls, IDs in config.yaml) — three calls per run:
        │       • transcribe: audio → word-level transcript with timestamps
        │       • plan: transcript → structured edit_plan.json (word indices, never cut times)
        │       • visuals: concept keywords → batched icon pick (or none)
        │
        ├─► Iconify API (api.iconify.design) — free, no key, no card
        │       • searched per concept keyword at render time
        │       • returns matching SVG icon — no pre-built asset library needed
        │
        └─► FFmpeg — deterministic rendering (see §5)
        │
        ▼
Final files delivered back via Telegram
```

**Silence detection is deterministic**, not model judgement: word-level transcript timestamps drive gap rules (600/350 ms + 120 ms pad, adaptive median) in code, cross-checked against energy-based silence intervals on the raw track (whisper stretches word timings across real pauses). The LLM never emits cut times.

**Why this shape:** nothing is "always running" except a near-zero-cost edge function (the Worker). Compute only exists for the few minutes it takes to process a video. This satisfies "free and active 24/7 whenever needed" without paying for, or needing a card for, persistent hosting.

**File-size note:** the Telegram Bot API caps file *downloads* at 20MB. For raw video input larger than that, don't route the raw file through Telegram — have the owner drop it in a linked storage location (e.g. a Drive/R2 link) and send the bot the link; use Telegram normally for control messages and for delivering the (usually smaller) final output.

---

## 5. Rendering pipeline (built for partial re-edits)

Do **not** render the whole video in a single FFmpeg pass. Split rendering into independently-patchable layers and segments, driven entirely by `edit_plan.json`:

```
edit_plan.json → segments: [{start, end, cuts, visual_query}, ...]
     │
     ├─ Layer 1: Base cut (silence removal + audio cleanup) — per segment
     ├─ Layer 2: Concept visual overlay (icon fade in/out) — per segment
     └─ Layer 3: Captions — applied last, at final mux, NOT baked per-segment
```

- Each segment renders as its own small clip.
- `final_video.mp4` = concat of all segment clips (stream-copy concat when codecs match — lossless, near-instant).
- Captions live in a separate layer applied at the very end so caption-only fixes never touch the video stream at all.
- **Cross-run state** travels as a small artifact (`edit_plan.json`, transcript, markers — never media); media is re-derived from the source on every run. Partial *decision* reuse is what matters: a fix never re-runs transcription or planning.
- Correction-loop note: segments are re-rendered in full on a fix (deterministic, unchanged segments come out identical); byte-for-byte segment reuse across runs is a documented future optimization, not an MVP dependency.

**This is what makes targeted correction possible.** A correction like "place diagram.png at 0:20" or "fix the caption at 0:45" maps to one segment or one caption-range in `edit_plan.json` via a structured patch op (`add_visual | remove_visual | recaption | retime_visual`, plus legacy `replace_icon` when icons are enabled); only that piece of the *plan* changes, transcription and planning are untouched, and the video is re-rendered deterministically from the patched plan. Silence cutting is automatic and can't be re-cut by a fix — surviving pauses get the cut settings tuned instead.

---

## 6. Visual strategy (no asset library, ever)

> Project choice (`config.yaml visuals.icons_enabled: false`): **no icon
> overlays at all — only input images are placed.** The Iconify path below
> stays implemented behind the flag, but the planner is instructed to emit
> screenshot-kind visuals only, assembly drops any icon draft with a note,
> and the correction loop rejects icon replacements.

Concept keywords come from the plan model's structured output (§4). For each keyword:

1. Search Iconify with pinned prefixes (`lucide,ph`) and keep a shortlist of up to 8 candidates (`https://api.iconify.design/search?query=<keyword>&prefixes=lucide,ph` — free, no auth, 200,000+ icons across 200+ open icon sets).
2. One batched LLM call picks the single best icon per concept from its shortlist — **or `null`** when nothing fits (best-of-8-or-none; never a forced bad pick).
3. Fetch the chosen SVG, render it to PNG (`rsvg-convert` primary), and overlay it as a simple fade-in/fade-out element near the frame edge — not a full animated diagram. This matches the "raw" style goal and keeps the render step cheap and simple.
4. Deterministic caps apply after the pick: ≤1 visual per 8 s density window and a max-visuals limit, so a confident-but-wrong model answer still cannot overcrowd the video.
5. User-attached screenshots may also be placed directly (kind `screenshot`) when the plan references a listed filename.

No JSON mapping table of concept → asset file is maintained by hand. If the model surfaces a brand-new concept it's never seen before, the search still works — vocabulary is unbounded by construction.

*(Future, explicitly out of scope for now: AI-generated custom diagrams via an image-gen model, for cases a flat icon doesn't represent well.)*

---

## 7. MVP scope

**Build this first, nothing more:**
- Input: `raw_video.mp4` (+ optional prompt/assets)
- Output: `final_video.mp4`
- Pipeline: transcribe → detect silence → extract concepts+timestamps → icon search per concept → segmented FFmpeg render (3 layers) → concat → captions muxed on top
- Delivery: Telegram in, Telegram out
- Human role: review, optionally request a targeted fix ("redo the visual at 0:20"), publish

**Explicitly deferred (do not build yet):**
- AI-generated (vs. found) diagrams/animations
- Automatic thumbnail generation
- Automatic title/description generation
- Multiple output formats/presets
- Learning from the owner's corrections over time
- Timeline preview before rendering
- Automatic publishing to the platform itself

---

## 8. Free-tier reference (verified, subject to provider changes)

| Service | Role | Free limit | Card required |
|---|---|---|---|
| Cloudflare Workers | Always-on webhook dispatcher | 100,000 requests/day | No |
| GitHub Actions | Burst render compute | Unlimited minutes on a public repo, 6h max job | No |
| LLM provider (Groq; Gemini supported) | Transcription + plan + icon pick | Groq: per-minute rate limits (backoff retries); Gemini free tier proved ~20 requests/day for this use | No |
| Iconify API | Concept → icon lookup | 200,000+ icons, public API, no published hard cap on the free public endpoint | No |
| Telegram Bot API | Control channel + delivery | File download capped at 20MB (use a link for larger raw input) | No |

---

## 9. Review/correction workflow

1. Owner receives `final_video.mp4` + `edit_report.md` via Telegram.
2. Owner either approves (publish) or replies with a targeted note, e.g. "place diagram.png where I say unit test" or "recaption 0:45–0:50."
3. The correction is mapped to a structured patch op over `edit_plan.json`
   (`add_visual | remove_visual | recaption | retime_visual`); anything else
   is answered with supported phrasings and no render.
4. The plan is patched and re-validated; transcription and planning are never
   re-run. The video is re-rendered deterministically from the patched plan.
5. New `final_video.mp4` is sent back with an updated report and state ref, so
   corrections can chain.

This loop is fast (one patch + re-render, no transcription/plan calls), since
only the plan changes — the pipeline itself stays deterministic.
