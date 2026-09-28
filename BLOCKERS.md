# Blockers (need the owner)

## 1. ~~SAMPLE_VIDEO_URL never supplied — real Drive-link path untested~~ RESOLVED 2026-09-27

- Owner supplied a real Drive link; run `36310143423` downloaded it via
  `gdown` (`[acquire] url source 1280x720 94.2s`), cut 94.2s → 86.9s kept,
  QC PASS, `final.mp4` (40.1MB) delivered to Telegram.
- First attempt failed on `TypeError: download() got an unexpected keyword
  argument 'fuzzy'` (gdown 6.x removed it) — fixed in `vedit/acquire.py`
  with a regression test pinning kwargs to the installed signature.
- Still untested: the worker's **>20 MB attachment → "send a Drive link"**
  reply (no >20 MB video was ever sent to the bot).

## 3. SESSION HANDOFF 2026-09-27 — worker batch-mode deploy PENDING (clean stop point)

**Milestone reached:** all batch-mode code is committed, pushed (`61b2d44`),
CI green (`test.yml` success on `61b2d44`). Python pipeline already supports
everything (cover images, fade/rise/drift, placement prompt, `add_visual`).

**What's live:** worker v4 — immediate dispatch + KV image staging + UA fix +
diagnostics. Fingerprint: srclen **8195**, djb2 **0x4885f78f**
(deployment `f75dff20`, 2026-09-27T11:18:53Z). Bot runs in immediate mode
tonight: send video → instant run; staged images ride along. Do NOT send
`done` yet (replies with usage help).

**What's pending:** deploy batch-mode worker = repo `infra/worker.mjs` @
`61b2d44`. Fingerprint: srclen **14991**, djb2 **0x57ae6541**.

**Deploy method that works** (hand-transcribed base64 does NOT — sandbox
blocks raw.githubusercontent.com, api.github.com, cdn.jsdelivr.net):
server-side patch via `cloudflare-personal_execute`: GET
`/accounts/60d867f2eaa9a1dababb2d248d2768aa/workers/scripts/vedit-relay/content/v2`
→ string-replace on the live source → djb2 hash gate → multipart PUT.
Keep secrets + bindings on PUT: plain_text `GH_OWNER`/`GH_REPO` +
kv_namespace `PENDING_IMAGES` = `f7c38d3fb974456d97783e2c41c61318`,
`compatibility_date: 2026-09-01`, `main_module: worker.mjs`.
Precheck live == v4 (len 8195 / 0x4885f78f) or abort. Old-string anchors
(all count-verified unique in v4) re-derive from `git show dbafe0e:infra/worker.mjs`.

**Verified new-segment hashes** (transcribe → hash-check each before PUT):
N1 0x870d6a97 · N2 (file L114-159) 0x37781f65 · N3 (L160-172) 0x3da5a436 ·
N4 (L174-208) 0x7a849bc7 · N5a (L210) 0xbd6371c6 · H1 (L224-228) 0x9da20ce9 ·
H2 (L229-237) 0x6454de2a, N5b = H1+"\n"+H2 · N6 (L239-246) 0x496cb18c ·
N7a (L260-291) 0xce588679 · N7bA (L293-343) 0xfa644750 ·
N7bB (L344-393) 0x541549da, len 1776 · N8 (L1-21) 0x44e99d67.
djb2 = `h=5381; h=((h*33)+ord(c))&0xFFFFFFFF` per char (BMP file, so
JS charCodeAt matches). In JS template literals escape backtick→\`,
`${`→\${, every regex backslash→double.

**Known pitfall:** N7bB re-transcription slipped once (1813 chars,
0x23f2cc1b vs expected 1776 / 0x541549da). Re-transcribe ONLY that span
from repo lines 344-393, verify hash, then assemble.

**After deploy:** live test via synthetic updates (Telegram secret path +
header from keys.txt): photo-with-caption stage → video stage → `done` →
watch `edit.yml` run → confirm `[attach] image-1.jpg`, placement in plan,
QC PASS, delivery. Then tell the owner batch mode is live.

**SESSION CONSTRAINT 2026-09-28:** the `cloudflare-personal` MCP tool is
absent from this session's toolset, and there is no other Cloudflare write
path here — no `CLOUDFLARE_*` env vars, no wrangler config dirs, wrangler
not installed (`npx wrangler whoami` hangs; OAuth login would need a
browser anyway). The deploy above can only run where the
`cloudflare-personal` MCP tools are loaded (they carry the auth). Resume
there with one deploy call; everything else is done and green.

## 2. Resolved during this session (kept for the record)

- Telegram relay dispatch failed with `github dispatch 403: Request
  forbidden by administrative rules … User-Agent header`. Root cause:
  Cloudflare Workers `fetch` sends no `User-Agent`, which the GitHub
  REST API requires. Fixed by pinning `"User-Agent": "vedit-relay"` in
  `infra/worker.mjs` (`dispatch`) and redeploying to the personal
  account. Verified: synthetic Telegram updates now create real
  `edit.yml` runs.
