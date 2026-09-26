# Blockers (need the owner)

## 1. SAMPLE_VIDEO_URL never supplied — real Drive-link path untested

- What is missing: a Google Drive "anyone with the link" URL of a real
  20–90 s talking-head recording (the mission's `SAMPLE_VIDEO_URL`).
- Consequence: the **Drive-link input path** (`gdown` branch in
  `vedit/acquire.py`) has never run against a real Drive file. All live
  runs used either a direct HTTPS release asset or a Telegram attachment.
  The `gdown` call itself is standard usage (`fuzzy=True`) and the
  surrounding pipeline is proven, but a first real-Drive run should be
  watched once a link exists.
- Related untested branch: the worker's **>20 MB attachment → "send a
  Drive link"** reply (no >20 MB video was ever sent to the bot).
- How to unblock: paste a Drive share link in chat (or send it to the
  bot) — the bot will dispatch `kind=new` with `url=` and the run needs
  no further input.

## 2. Resolved during this session (kept for the record)

- Telegram relay dispatch failed with `github dispatch 403: Request
  forbidden by administrative rules … User-Agent header`. Root cause:
  Cloudflare Workers `fetch` sends no `User-Agent`, which the GitHub
  REST API requires. Fixed by pinning `"User-Agent": "vedit-relay"` in
  `infra/worker.mjs` (`dispatch`) and redeploying to the personal
  account. Verified: synthetic Telegram updates now create real
  `edit.yml` runs.
