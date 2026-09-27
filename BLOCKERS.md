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

## 2. Resolved during this session (kept for the record)

- Telegram relay dispatch failed with `github dispatch 403: Request
  forbidden by administrative rules … User-Agent header`. Root cause:
  Cloudflare Workers `fetch` sends no `User-Agent`, which the GitHub
  REST API requires. Fixed by pinning `"User-Agent": "vedit-relay"` in
  `infra/worker.mjs` (`dispatch`) and redeploying to the personal
  account. Verified: synthetic Telegram updates now create real
  `edit.yml` runs.
