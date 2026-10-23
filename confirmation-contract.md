# Worker confirmation contract — `vedit fix` dry-run / apply-preview

The Telegram worker (or any automation) must confirm destructive or
timestamped edits with the owner **before** applying them. This repo provides
the mechanism; the worker owns the conversation. Recommended policy: always
confirm fix ops via dry-run; keep direct `vedit fix` for CLI power use.

Times in previews and confirmations are **input-video (source) seconds**,
with the corresponding **final-video seconds** alongside — state both, every
time, so "input or final?" is never ambiguous.

## Production wiring (infra/worker.mjs + edit.yml)

Slash commands are deterministic (never the LLM): `/cut`, `/keep`, `/clip`,
`/place`, `/move`, `/remove`, `/size`, `/recaption`, `/change`, `/preset`
preview instantly (`kind=fix-dryrun`, exactDK instruction strings, e.g.
`/size image 3 60%` → `resize image 3 60%`, `/preset punchy` →
`preset punchy`); `/apply` `/reject` `/redo <sha8>` `/deliver` `/status`
`/list` `/cancel` manage the conversation; `/new` collects the video (link =
Drive URL, direct <20MB upload), `/addimage` stages photos (caption time
optional), `/build` (or plain `done`) starts the run; `/editimage [N]` opens
an aspect/crop/size menu whose buttons emit exact directives
(`set_visual_mode image-3 cover crop top`, `resize image-1 to 60%`,
`preset clean_voice`) parsed by `vedit.stage.fix.parse_directive` with no
model call. The operator menu is registered lazily via `setMyCommands`
(runs once, cached in KV for 7 days). `/status` polls the edit.yml runs API
anchored on the newest dispatch (KV `last_run:<chat>`, 6h).

The talker (`CHAT_MODEL = openai/gpt-oss-20b` via Groq, `GROQ_API_KEY`
secret) classifies only free text — `chat` (reply directly, no pipeline),
`fix`/`adjust` (dispatch dry-run), `unknown` — with dialogue memory
(`conv:<chat>`, last 6 turns, 1h TTL) and the latest delivered state resolved
on demand via the GitHub artifacts API (latest `state-<sha8>`), so
corrections never need a reply-to message. Strict yes/no affirmations are
rule-based ONLY when a preview is pending (`pending_confirm:<chat>`, 30
min) — small talk can never silently apply an edit; the LLM never decides
confirmations. Staging (`done`/`cancel`/URLs/images) — dry-run previews
never consume staging; only `new` runs do. Voice messages are politely
declined (transcription policy), stickers get a hint to resend as photos.
Without `GROQ_API_KEY` the worker keeps routing commands and callbacks, and
free text gets usage help.

Inline keyboard: dry-run confirmations carry `apply:<sha8>` /
`reject:<sha8>` buttons (callback queries answered on the worker side, then
dispatch `kind=fix-apply`); the typed yes/no rules still work.

`edit.yml` modes: `new` / `fix` (unchanged) + `fix-dryrun` (dry-run, posts
the confirmation + tagged frame to Telegram from the workflow, uploads
`preview-<state_ref>`) + `fix-apply` (downloads state + preview artifacts,
`--apply-preview`, delivers as usual). State artifacts are never uploaded by
dry-run runs.

Redelivery without re-render: `kind=deliver` downloads the latest video
artifact (`video-<sha8>`) when it still exists — the file already exists —
otherwise re-runs `vedit deliver` from the latest state artifact (QC-gated).
The talker sends it for resend intents ("send it again", "I didn't get the
video") with no preview step; the rule-based redeliver regex catches typed
requests too. Delivery failures persist `last_error.txt`, which the workflow
failure step sends instead of the generic message. Successful renders also
upload a short-lived `video-<sha8>` artifact (`final.mp4` + preview +
contact sheet, 7-day retention); the artifact expiring back to "re-run
apply".

## Commands (run in the worker sandbox, same `--config`)

1. Preview (never mutates run state except `fix_preview.json` + `preview/`):
   `vedit fix --state <state> --instruction "<free text>" --dry-run [--chat-id …]`
   - exit `0` = preview ready, `2` = cannot preview (`ok: false` in JSON).
2. Apply (no LLM call; re-resolves the saved instruction when the plan moved
   on, with a note — the YES authorized the instruction, not the byte-plan):
   `vedit fix --state <state> --apply-preview [--chat-id …]`
   - `SystemExit("no fix_preview.json …")` when no preview exists.
   - Re-resolution failures surface as FixErrors; a preview without any
     instruction still requires a fresh step 1.

## `state/fix_preview.json` schema

```json
{
  "ok": true,
  "instruction": "cut 0:30 to 0:45",
  "op": "cut_range",
  "patch": {"op": "cut_range", "from_word": 143, "to_word": 160, "...": null},
  "plan_hash": "9f2c…",
  "input_range_s": [30.0, 45.2],
  "output_range_s": [20.1, 35.3],
  "phrase": "stay focused on…",
  "affected": {
    "segments_before": 12, "segments_after": 13,
    "visuals_removed": ["v4"], "visuals_added": [],
    "captions_before": 40, "captions_after": 37,
    "zooms_before": 2, "zooms_after": 2
  },
  "duration": {"before_s": 55.0, "after_s": 40.2},
  "notes": ["cut words 143..160 from segment …", "…"],
  "frame": "preview/fix_preview_1.jpg"
}
```

On failure: `{"ok": false, "instruction": …, "op": "<op>" | null,
"patch": {…} | absent, "plan_hash": …, "error": "<FixError text>"}` and no
`frame`. `frame` is absent when tagging failed (best effort) — confirm from
the times + phrase alone. `input_range_s`/`output_range_s`/`phrase` are absent
when the op carries no word range (e.g. `remove_visual` by id).

## Confirmation message

Send `confirmation_text` semantics (see `vedit/stage/fix.py`): op in plain
words, **input range + final-video range**, covered phrase, affected
overlays/captions, duration delta, tagged frame attached when present, ending
with an explicit YES/NO prompt. Suggested worker flow:

1. owner sends correction → worker runs step 1 (dry-run).
2. worker sends the confirmation message + `frame` (if present).
3. owner replies YES → worker runs step 2 (apply-preview) → pipeline
   re-renders, QC gates, delivery as usual. Owner replies NO / anything else
   → discard (optionally delete `fix_preview.json` + `preview/`).

`fix_preview.json` and `state/preview/` never touch stage markers, so
previewing never disturbs resumability.
