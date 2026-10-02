# vedit — AI video editing agent

Turns one raw talking-head recording into one publish-ready **1080×1920** Reel:
silence-aware jump cuts, noise-reduced audio, burned captions, concept icons —
then delivers it to your Telegram. Corrections happen by replying to the bot.

AI decides *what* to cut and *what* to show; deterministic code decides *how*
(FFmpeg, word-gap rules, a hard QC gate). The LLM never emits cut timestamps.

## How a run works

```
Telegram ─► Cloudflare Worker ─► GitHub Actions (workflow_dispatch)
                                    ├─ acquire   (Drive link / TG file / URL → source.mp4)
                                    ├─ audio     (denoise + normalize)
                                    ├─ transcribe (word-level timestamps)
                                    ├─ cut       (deterministic gap rules → segments)
                                    ├─ plan      (LLM: visuals/captions/zooms, word indices)
                                    ├─ visuals   (Iconify shortlist → batched pick → PNG)
                                    ├─ captions  (.srt + .ass in output time)
                                    ├─ render    (reframe → per-segment encode → concat → burn)
                                    ├─ qc        (hard gate: duration, streams, coverage, assets)
                                    └─ report + deliver (edit_report.md, contact sheet, preview, video)
```

State travels between runs as a small GitHub artifact (`state-<sha8>.zip`:
JSON/MD/SRT/ASS + markers — **never media**). A fix run patches
`edit_plan.json` (structured op) and re-renders; it never re-transcribes or
re-plans.

## Repository map

| Path | What |
|---|---|
| `vedit/` | Python package — all business logic lives here |
| `vedit/stage/` | One module per pipeline stage (`plan`, `cuts`, `render`, `qc`, `fix`, …) |
| `config.yaml` | **Every** tuning knob: cuts, audio, video, models, retry, limits |
| `.github/workflows/edit.yml` | The pipeline (`workflow_dispatch`, 8 string inputs) |
| `.github/workflows/test.yml` | Lint + pytest gate on every push / PR / manual run |
| `infra/worker.mjs` | Telegram webhook Worker (allowlist → dispatch → reply handling) |
| `infra/set_webhook.sh` | Registers the webhook with Telegram (secret_token) |
| `keys.txt` | Local secrets (gitignored — never commit it) |

## Cold setup (clean machine → working bot)

Prereqs: `ffmpeg` (with `ass` filter), Python 3.12, Node 24, `git`, `gh`.

1. **Clone + Python env**

   ```bash
   git clone https://github.com/vaibhavs-cloud/video_edit.git
   cd video_edit
   python -m venv .venv
   .venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
   ```

2. **Secrets** — create `keys.txt` (gitignored) with:

   ```
   GEMINI_API_KEY=...        # fallback provider (optional if provider: groq)
   GROQ_API_KEY=...          # active provider (see config.yaml models.provider)
   TELEGRAM_BOT_TOKEN=...    # @daemon7010_bot
   TELEGRAM_CHAT_ID=...      # your chat id (allowlist)
   GH_TOKEN=...              # classic PAT, scope repo
   GH_OWNER=vaibhavs-cloud
   GH_REPO=video_edit
   TELEGRAM_SECRET_PATH=...  # webhook path segment
   TELEGRAM_SECRET_TOKEN=... # Telegram secret_token header value
   ```

3. **Local smoke test (no secrets needed)**

   ```bash
   set PYTHONPATH=.
   .venv/Scripts/python -m pytest -q
   .venv/Scripts/python -m ruff check vedit tests
   .venv/Scripts/python -m ruff format --check vedit tests
   .venv/Scripts/python -m vedit process --mock --name smoke
   ```

4. **GitHub**

   ```bash
   gh auth login
   gh secret set GEMINI_API_KEY    < keys.txt   # or: paste per-value
   gh secret set GROQ_API_KEY
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   ```

   Secrets required by `edit.yml`: `GEMINI_API_KEY`, `GROQ_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

5. **Worker (Cloudflare personal account)**

    The Worker source is `infra/worker.mjs` with `infra/wrangler.toml`
    (vars: `GH_OWNER`, `GH_REPO`; secrets: `TELEGRAM_BOT_TOKEN`,
    `TELEGRAM_SECRET_PATH`, `TELEGRAM_SECRET_TOKEN`, `GH_TOKEN`,
    `ALLOWED_CHAT_ID`, `GROQ_API_KEY`). Deploy with wrangler (or the Cloudflare MCP), then:

   ```bash
   ./infra/set_webhook.sh
   curl https://vedit-relay.<subdomain>.workers.dev/health   # → ok
   ```

6. **Use it from Telegram** (batch mode — nothing runs until `done`)

   - Send the **video** (≤20 MB) or a **Drive share link** — the bot holds
     it and confirms. Optionally add instructions alongside.
   - Send **images**, each captioned with a label + placement in any
     timestamp format: `pyramid at 0:20` · `diagram 0:45-0:50` ·
     `chart @1m20s`. No caption = planner places it where it fits.
     Images fill the whole frame (center-cropped) with subtle varied
     transitions (fade / rise / drift); no floating icons, ever.
   - Send **`done`** — v1 builds once with everything placed.
     `cancel` clears the staging area.
    - Receive `final.mp4` + report caption ending in `state <sha8>`.
    - **Just talk to it** — no reply-to or keywords needed:
      `that image at 30 is wrong, move it to 40` · `cut the boring intro` ·
      `make the title punchier`. The bot previews every edit with input +
      final-video times and a tagged frame — reply **YES** to apply.
      (Reply-to-a-video still works for corrections too.)
    - Unsupported instructions get a hint message — nothing re-renders.

### CLI (local runs)

```bash
python -m vedit process --input <url|tg:file_id|path> --name run --chat-id <id>
python -m vedit fix --state out/run --instruction "use a shield instead"
python -m vedit qc --state out/run
python -m vedit report --state out/run
```

Stages are marker-gated (`.done.<stage>`): an interrupted run resumes where it
stopped; `--from-stage <stage>` forces a re-run from any point.

## Configuration

`config.yaml` is the only place tuning knobs live:

- `models.provider` — `groq` (default) or `gemini`; model IDs alongside it
- `cuts` — silence gap rules (600/350 ms, 120 ms pad, adaptive median)
- `audio`, `video`, `captions`, `visuals` (density cap, screenshot width;
  `icons_enabled: false` — only input images are placed, never stock icons)
- `qc` — duration tolerance 0.25 s, caption coverage ≥ 0.95
- `retry` — LLM/HTTP attempt counts and backoff
- `limits` — input/output caps, Telegram 20 MB download / 50 MB send

## Testing

- `pytest` — unit (cut rules, timeline, captions, plan sanitising, fix ops,
  LLM fallbacks) + golden `--mock` end-to-end render with QC assertions
- CI (`test.yml`) runs `ruff check` + `ruff format --check` + `pytest` on every
  push; `edit.yml` is the live pipeline (20-minute timeout, state artifacts
  retained 30 days)

## Design rules (don't violate)

1. The LLM never emits cut timestamps — word indices only, validated by
   pydantic; 1 repair retry, then degrade to captions-only (never crash).
2. AI decides *what*, deterministic code decides *how*.
3. All knobs in `config.yaml`; model IDs never hardcoded in code.
4. Secrets live only in `keys.txt` (local) / GitHub Secrets / Worker vars —
   never in code, logs, or commit messages.
5. State artifacts store small files only; media is always re-derived.
