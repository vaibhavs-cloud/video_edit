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
3. **Single LLM provider.** Do not split reasoning and transcription across multiple AI providers. One provider must handle both transcription and concept/edit-plan reasoning, to avoid multi-provider fragility (one provider failing should not be able to break the whole pipeline). Current choice: **Gemini** (via Google AI Studio free tier) — it accepts audio directly, so transcription and reasoning happen in the same call/provider.
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
        │  (fires a repository_dispatch event)
        ▼
GitHub Actions  — burst compute, free tier (2,000 min/month on a private repo)
        │
        ├─► Gemini (single provider) — audio in, one call:
        │       • transcript
        │       • silence/pause timestamps
        │       • concept + keyword extraction with timestamps
        │       • per-concept visual query terms
        │       • structured edit_plan.json
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

**This is what makes targeted correction possible.** A correction like "the icon at 0:20 is wrong" or "fix the caption at 0:45" maps to one segment or one caption-range in `edit_plan.json`. Only that piece gets regenerated; everything else is untouched and re-used byte-for-byte in the concat step.

---

## 6. Visual strategy (no asset library, ever)

Concept keywords come from Gemini's structured output (§4). For each keyword:

1. Query `https://api.iconify.design/search?query=<keyword>` (free, no auth, 200,000+ icons across 200+ open icon sets).
2. Pull the top matching icon's SVG.
3. Overlay it as a simple fade-in/fade-out element near the frame edge — not a full animated diagram. This matches the "raw" style goal and keeps the render step cheap and simple.

No JSON mapping table of concept → asset file is maintained by hand. If Gemini surfaces a brand-new concept it's never seen before, the search still works — vocabulary is unbounded by construction.

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
| GitHub Actions | Burst render compute | 2,000 min/month (private repo), unlimited on public repo, 6h max job | No |
| Gemini API (Google AI Studio) | Transcription + concept/edit-plan reasoning | ~1,500 requests/day (Flash tier) | No |
| Iconify API | Concept → icon lookup | 200,000+ icons, public API, no published hard cap on the free public endpoint | No |
| Telegram Bot API | Control channel + delivery | File download capped at 20MB (use a link for larger raw input) | No |

---

## 9. Review/correction workflow

1. Owner receives `final_video.mp4` + `edit_report.md` via Telegram.
2. Owner either approves (publish) or replies with a targeted note, e.g. "fix the icon at 0:20" or "recaption 0:45–0:50."
3. The correction is mapped to the relevant segment(s)/caption-range in `edit_plan.json`.
4. Only that piece is regenerated (Layer 2 or Layer 3, one segment) — not a full re-render.
5. New `final_video.mp4` is reassembled via concat and sent back.

This loop should be fast (seconds to low minutes), since only a fraction of the video is ever actually re-rendered.
