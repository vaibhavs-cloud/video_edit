# Worker confirmation contract — `vedit fix` dry-run / apply-preview

The Telegram worker (or any automation) must confirm destructive or
timestamped edits with the owner **before** applying them. This repo provides
the mechanism; the worker owns the conversation. Recommended policy: always
confirm fix ops via dry-run; keep direct `vedit fix` for CLI power use.

Times in previews and confirmations are **input-video (source) seconds**,
with the corresponding **final-video seconds** alongside — state both, every
time, so "input or final?" is never ambiguous.

## Production wiring (infra/worker.mjs + edit.yml)

Free text needs no reply-to and no keywords. The Worker (`CHAT_MODEL =
llama-3.1-8b-instant` via Groq, `GROQ_API_KEY` secret) classifies each message
— `chat` (reply directly, no pipeline), `fix`/`adjust` (dispatch dry-run),
`yes`/`no` (apply/discard pending) — with dialogue memory (`conv:<chat>`,
last 6 turns, 1h TTL) and the latest delivered state resolved on demand via
the GitHub artifacts API (latest `state-<sha8>`), so corrections never need
a reply-to message. Pending confirms live in KV (`pending_confirm:<chat>`,
30 min). Staging (`done`/`cancel`/URLs/images) is unchanged; without
`GROQ_API_KEY` the worker keeps legacy behavior (reply-to dispatches
`kind=fix` immediately, anything else gets usage help).

`edit.yml` modes: `new` / `fix` (unchanged) + `fix-dryrun` (dry-run, posts
the confirmation + tagged frame to Telegram from the workflow, uploads
`preview-<state_ref>`) + `fix-apply` (downloads state + preview artifacts,
`--apply-preview`, delivers as usual). State artifacts are never uploaded by
dry-run runs.

Redelivery without re-render: `kind=deliver` downloads the latest state
artifact and runs `vedit deliver` (QC-gated). The talker sends it for resend
intents ("send it again", "I didn't get the video") with no preview step —
the file already exists. Delivery failures persist `last_error.txt`, which
the workflow failure step sends instead of the generic message. Successful
renders also upload a short-lived `video-<sha8>` artifact (`final.mp4` +
preview + contact sheet, 7-day retention) so `kind=deliver` downloads the
video instead of re-rendering; the artifact expires back to "re-run apply".

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
