# BUG_HUNT.md — vedit audit, 2026-10-02

Method: three parallel scoped audits (render/audio/cuts/timeline/qc/captions ·
cli/state/schema/acquire/security · llm/plan/fix/visuals/transcribe/report),
then every HIGH and MEDIUM claim re-verified against the code; the
`[EXEC]`-marked ones additionally reproduced by running code.
Counts: **7 HIGH · 16 MEDIUM · LOW/polish** (grouped). Test gaps + fix waves at the end.

Out of scope by owner decision: fix-instruction time-base semantics
(source vs output timeline) — held back as H8 pending owner recheck, not in this file.

---

## 🔴 HIGH

### H1 — Stale state silently wins: re-`process` with new input/prompt/config is a no-op
`vedit/cli.py:526-552` builds the run from CLI args, but `_run_stages`
(`cli.py:435-441`) skips every stage with a `.done.*` marker (`state.py:57-63`;
`ensure_state` is mkdir-only, `state.py:73-74`). A second
`process --input new.mp4 --prompt "new"` keeps old `source.mp4`,
`transcript.json`, `edit_plan.json` and exits on the **old** `qc.json`
(`cli.py:549`). The e2e suite even asserts this (`test_mock_process.py:65-66`).
Related facets, same root cause (stored run params are never compared):
- `config_hash` is written to `meta.json` (`cli.py:193`) but has **zero readers**
  (only other match is the `schema.py:238` default) — config changes never
  invalidate anything.
- `--from-stage render` + new `--input`: `clear_from(state, "render")`
  (`cli.py:530-531`) leaves `acquire` marked done, so the new input is never ingested.
- `cmd_process` re-run with `--attachments`: `if not target.exists(): copytree`
  (`cli.py:532-537`) silently keeps the old attachments dir.
- `audio.prepare` existence-gates `raw/clean/stt` (`audio.py:107,122,137`) and
  `ensure_base` returns any non-empty `base_<sha8>.mp4` (`render.py:54-58`,
  keyed on source hash only — no `cfg.hash`, geometry, fps, preset, crf or
  reframe mode, unlike `_segment_fingerprint` at `render.py:256-304` which does
  include them). Changing `volume_gain_db`, `channels`, `width`, `fps` … then
  resuming reuses the old artifacts: silent wrong output. Same for
  `_make_mock_source` (`cli.py:132-134`, existence-gated).
Fix: store `input_ref`/`prompt`/`mock`/`cfg.hash` in `meta.json`; on mismatch
abort ("use a new `--name` or `--from-stage acquire"`) or auto-clear; fingerprint
audio/base caches with sidecars like segments.

### H2 — QC gate passes vacuously on empty/truncated `qc.json` `[EXEC]`
`QcResult.passed = not self.hard_failures` (`qc.py:29-30`); verified:
`QcResult(checks=[]).passed is True`. `_qc_result` (`cli.py:124-127`) does
`QcResult(checks=data["checks"], …)` with no non-empty/schema check (missing file
→ traceback instead of clean FAIL; truncated `{"checks": []}` → pass), and
`cmd_deliver` relies on it (`cli.py:633-634`).
Fix: validate `checks` non-empty with `name/ok/severity`, require the hard
checks (`final_exists`, `has_streams`) present, else `StateError("QC has not passed")`.

### H3 — `burn_captions` omits `-y` and codec pins: fix re-renders silently keep the OLD `final.mp4` `[EXEC]`
`burn_captions` (`render.py:557-570`) is the only ffmpeg call in `render.py`
without `-y` (cf. lines 83, 319, 443, 510) and without `-c:v/-preset/-crf/
-pix_fmt/-r` pins. Reproduced: on an existing output ffmpeg prints
`Not overwriting - exiting / Error opening output file`, leaves the file
**untouched, and exits 0** — so `ff.run` (non-zero check only, `ff.py:26-42`)
raises nothing and `burn_captions` returns success. Every `vedit fix` on a
completed state (`clear_from(state, "visuals")`, `cli.py:591-598`) therefore
delivers the **pre-fix video**; QC can't catch it (duration/resolution/fps/
coverage are unaffected by visual-only plan edits), and the e2e fix tests stay
green on the stale file. Second half: the final pass encodes with ffmpeg
defaults instead of the segment settings (`veryfast/crf20` per config) —
quality/bitrate discontinuity vs the segments.
Fix: add `"-y"` + mirror the segment/crossfade video flags.
**Status 2026-10-02: fixed in `render.burn_captions`** (`-y` + libx264 /
preset / crf / pix_fmt / fps pins, mirroring `_encode_one_segment`). Required
prerequisite: without it no cut/keep fix can ever deliver a fresh file
(proven by the e2e cut flow failing on the stale `final.mp4`).

### H4 — Captions are timed on the lossless timeline but burned onto the crossfade-shortened video
`build_lines` maps through lossless `TimelineMap` (`captions.py:49-89`);
`concat_crossfade` shortens the video by one overlap per join
(`render.py:468-504`, chained `xfade`+`acrossfade` in the same window — the A/V
math itself is correct); QC expects the shortened duration (`qc.py:57-63`).
The `.ass` is burned as-is onto the shortened video, so every caption after
`k` joins renders `k × overlap` late — with defaults (100 ms, 12 segments) up
to **~1.1 s** at the end, scaling with `crossfade_video_ms`. Edge compounding
it: the degenerate shrink rule (`render.py:481-483`,
`overlap = max(0.01, shortest - 0.05)`, still `> shortest` when
`shortest < 0.06`) is not shared with QC, which always models the full overlap.
Fix: one `crossfade_overlap_s()`-style helper (already exists at
`render.py:448-461` — use it in QC too) plus an effective-overlap that captions
subtract per join, e.g. `TimelineMap(segments, overlap_s)` or a post-adjust in
`build_lines`.

### H5 — `resolved.json` absolute-path escape on load
`_load_resolved` (`cli.py:110-121`): `asset = ctx.state / entry["asset"]` —
`Path("out/x") / "/etc/hostname"` collapses to the absolute path (verified),
with no `is_absolute()`/`is_relative_to` check and no key/type validation
(corrupt entries → `KeyError`). A tampered manifest passes `asset.exists()`
and is fed to ffmpeg and counted resolved. The save side *does* filter with
`is_relative_to(ctx.state)` (`cli.py:353-357`) — the load side must match it.
Fix: reject absolute entries; resolve (non-strict) and require containment in
`state`; skip + warn otherwise; validate keys/types.

### H6 — Telegram bot token leaks into logs
`Telegram._ok` raises `f"telegram {resp.url.path}: …"`
(`telegram_client.py:30-36`); for `bot<TOKEN>/getFile` the path **contains the
token**. `_notify` catches and `_warn`s it (`cli.py:59-66`), landing the token
in stdout/CI logs (same for any traceback stringifying the request URL).
Fix: raise `f"telegram {method}: {description}"` (pass method in); never log
URL/path; redact token from logged exceptions.

### H7 — Icon pipeline trusts the LLM string as path/URL (allowlist bypass)
`pick_icons` returns whatever the model emits with no membership check against
`shortlists_map` (`visuals.py:95-96`); `fetch_icon` splits on `:` and
interpolates into `cache_dir / f"{prefix}-{name}.svg"` and the Iconify URL
(`visuals.py:121-135`, only an empty-name check) — `../../x` escapes
`state/icons`, `a/b`/empty-prefix guarantee 404s. `IconPicks`
(`schema.py:197-203`) imposes no format. Companion defects, same function:
SVG cache checks existence only (line 131) while PNG checks size (line 129) —
a zero-byte SVG poisons that icon forever; `_get` retries **all** exceptions
including permanent 404s (`visuals.py:34-45`). Owner `--prompt`/`--instruction`
text shapes the pick prompt, so this is a trust-boundary gap, not just polish.
Fix: accept picks only if `in shortlists_map[vid]`; validate
`^[a-z0-9-]+:[a-z0-9-]+$`, reject `/`, `\`, `..`; refetch empty SVGs; don't
retry 4xx except 429.

---

## 🟡 MEDIUM

- **M1 — `_deliver` enforces nothing itself.** `cli.py:444-474`: missing
  `final.mp4` → `size 0` → `send_video` → `FileNotFoundError` on `open("rb")`;
  no `_qc_result` gate inside (relies on callers at 549-551, 600-602, 633-634).
  Fix: gate + existence/non-empty checks at the top of `_deliver`.
- **M2 — `resolve_screenshot` takes `""`, directories, and `..`.**
  `assets.py:52-63`: `attachments_dir / ""` **is** the dir → returned as an
  asset (reachable via valid `Visual(file=None, file_id=…)` — `schema.py:80-81`
  — since resolve uses `v.file or ""`, `visuals.py:161`); no `is_file()`,
  no separator/`..` rejection. Manifest filter drops escapes at save, but the
  read is attempted and a directory asset crashes render retries.
  Fix: reject empty/separator/`..`, require `is_file()`; resolve or reject `file_id`.
- **M3 — Plan assembly has no screenshot allowlist (fix does).**
  `assemble_plan` (`plan.py:204-238`) never checks `dv.file` against the
  screenshots list (only `fix.apply_patch:249` does). Mitigated downstream
  (manifest filter + resolve drop), but tighten at the boundary: basename-only
  + membership, else drop + note.
- **M4 — Acquire leaves partials, uncapped gdown, raw errors.**
  `acquire.py:43-62`: httpx 4 GiB abort raises without `unlink` (contrast
  `telegram_client.py:58`); gdown path has no size cap/timeout; transport
  errors propagate as raw `httpx.*`, not `AcquireError`.
  Fix: `try/finally` unlink, Content-Length pre-check vs a URL max, wrap errors.
- **M5 — Non-atomic saves + brittle loaders.** `save_json` is direct
  `write_text` (`state.py:77-92`); kill mid-write → truncated `qc.json`/
  `edit_plan.json` → next `_qc_result`/`_load_*` raises traceback instead of
  clean FAIL. Fix: tmp + `os.replace`; treat corrupt QC as `passed=False`
  with a message.
- **M6 — `attach --file-ids` parsing + unvalidated extension.**
  `cli.py:484-505`: `startswith("[")` without strip; bare `json.loads`
  traceback; `suffix or ".jpg"` accepts `.mp4/.svg/.exe` → stored as
  `image-N.*`, fails later in render. Fix: strip, catch `JSONDecodeError`,
  allowlist `{jpg,jpeg,png,webp}`.
- **M7 — Single-word visuals at a segment/transcript end are always dropped.**
  `assemble_plan` widens `hi == lo` strictly forward (`plan.py:212-213`) then
  runs the single-segment check on the widened range (218): end-of-segment →
  "crosses a cut"; last transcript word → clipped back → "empty range".
  Logged, narrow trigger (model emits `from == to` at a boundary), but the
  forward-only widening is wrong — try `(lo-1, lo)` before dropping.
- **M8 — Caps/density enforced only in `resolve_visuals`, silently.**
  `assemble_plan` and `apply_patch` never consult `max_visuals`/
  `density_window_s` (config: 12 / 8 s); `apply_limits` returns kept-only with
  no reasons (`visuals.py:103-118`), `on_error` fires only for missing assets
  (154-164). Plans/fixes report success (`fix: add_visual v13 …`), render
  drops the excess — visible only as a `resolved/planned` warn.
  Fix: enforce with notes in plan/fix, or return `(kept, dropped+reasons)`.
- **M9 — `apply_limits` indexes words before bounds-checking.**
  `transcript.words[v.from_word]` (line 111) runs before the only bounds
  filter (line 118), outside the per-visual `try` in `resolve_visuals`
  (147-169) — one bad visual crashes resolve, contradicting the "never fatal"
  docstring (line 146). Validated plans are in-bounds, so latent; pre-filter
  anyway.
- **M10 — One negative/NaN timestamp kills the transcript.** `_sec` passes
  negatives/non-finite through (`transcribe.py:25-36`); callers only skip
  `None` (62, 82); `Word` requires `ge=0` (`schema.py:16-26`) → single bad
  word raises `ValidationError`, discarding all good words. Fix: `None` for
  non-finite/negative in `_sec`, or per-entry `try/except`.
- **M11 — Gemini path concatenates every nested `words` block.**
  `_find_audio_blocks` collects all dicts with a `words` list
  (`transcribe.py:39-49`); `parse_response` concatenates + re-indexes — dupes
  if the SDK nests the same words (candidates/parts/metadata). Groq path
  (73-90) reads top-level only and is safe. Fallback-path only; take the best
  block or dedupe `(s, e, t)`.
- **M12 — Gemini helper's first attempt is a dead OpenAI-shaped dict.**
  `llm.py:101-113`: plain dict passed as `config=` fails (caught) before the
  real `GenerateContentConfig` — one wasted attempt per call; loop catches
  only `(TypeError, ValueError)` (130) so network errors skip the fallback;
  empty `.text` leaves `last=None` → `LlmError("…None")` (133).
  Fix: drop the dict entry, set `last` on empty text.
- **M13 — Caption coverage gaps fail late instead of degrading early.**
  `assemble_plan` (`plan.py:240-276`): out-of-range spans `continue` silently
  (visuals log every drop), overlaps/gaps pass through unchecked despite the
  prompt's "every word covered by exactly one span", straddle-splits are
  silent, 1–6-words-per-span unenforced — fallback fires only on **zero**
  captions. A 50%-covered model yields `degraded=False` then a hard QC
  `caption_coverage` failure (`qc.py:112-117`, min 0.95). Fix: note
  drops/splits, compute coverage, `degraded=True` below threshold.
- **M14 — Overlay fade-in runs while hidden (`st=0`), so mid-segment visuals pop.**
  `render.py:228-229` hardcodes `st=0` while `enable` starts at `local_start`
  (244-247); `fin > 0` exactly when the visual starts inside the segment
  (164) — the normal case. Fade-out is absolute (231-232) and correct.
  Fix: `st={ov.local_start}`.
- **M15 — Tall screenshots crash render in `overlay` mode.**
  `target_h` unconstrained (`render.py:141-156`); `_place("bottom")` puts
  `y < 0` → hard `RenderError`. Default `cover` hides it; switching to the
  supported `overlay` knob makes valid portrait input fatal.
  Fix: fit-inside scaling (`force_original_aspect_ratio=decrease` or min-ratio
  with placement-derived `max_h`).
- **M16 — Transcription retries permanent 4xx with full backoff.**
  Groq (103-126) and Gemini (146-161) catch generic `Exception` — a bad key
  (401) costs 4 attempts + backoff before surfacing (sleeps are correctly
  guarded, 121/160). `llm.py:90-92` already breaks on auth for chat —
  mirror that discrimination here.

---

## 🟢 LOW / polish (all code-verified)

- **ASS/time text:** `_ass_time` minute/hour carry missing — `[EXEC]`
  `59.9956 → "0:00:60.00"` (invalid; the literal `59.995` float-reprs just
  below the rounding edge, so the trigger is a ms-window per minute boundary —
  narrow but real; fix with total-centisecond `divmod` like `_srt_time`,
  `captions.py:28-46`); no ASS escaping — `{ } \` and newlines in tokens or
  `override_text` inject override blocks (`captions.py:80-87,54-66,167-169`;
  SRT safe via `text_plain`); caption line-width off-by-one after wrap
  (`captions.py:101-107`, `addition` keeps stale `+1` after `flush()`).
- **Overlay cosmetics:** `rise` hardcodes `/0.35` ignoring `fade_s`/`fx_dur`
  (`render.py:237,184,240`); `+12` border pad never drawn, just 6 px
  off-center (`render.py:146,171,227`); `afade d=0.01` on sub-20 ms slivers
  (`render.py:250-252`, negligible at default `min_kept` 0.5 s); fonts
  `copytree`-once never refreshes stale `work/fonts` (`render.py:546-550`).
- **Cuts knobs:** `adaptive_multiplier` is a no-op at shipped defaults
  (`cuts.py:84-91` + `600/350/1.8` → threshold always `0.6 = cut_gap`;
  effective only below ~1.71 — conservative, just document/retune);
  wordless-interval drops aren't reflected in the `kept` accounting used for
  the relax check (`cuts.py:160-181`; the `s_cut` log uses post-drop Segments
  so it stays correct); overlap-shrink vs QC can only disagree with
  non-default overlap (`render.py:481-483` vs `qc.py:57-63`;
  `min_kept 0.5 > overlap 0.1` at defaults).
- **Compat:** `alimiter …:latency=1` needs newer ffmpeg (`audio.py:91-94`;
  local 2026 build supports it — probe `ffmpeg -h filter=alimiter` or
  try/except-fallback for older systems).
- **Fix-loop warts:** `_mock_patch` `IndexError` on trailing `"use a "`
  (`fix.py:111`, mock-only); `"remove"` tested first so
  "remove … and place …" always parses as remove; retime/replace need an
  `icon` visual so screenshots-only mock never exercises them
  (`fix.py:105-137`); `_caption_rows` slices text with unclamped indices and
  `words[-1]` on empty transcript (`fix.py:51-69`); `add_visual` defaults to
  `segments[0]` only and reuses freed ids (`fix.py:252-272`); `recaption`
  ignores `to_word` and picks first overlapping caption (`fix.py:284-298`);
  gap words report "crosses a cut boundary" (`fix.py:237-238,267-268`).
- **Failure-path latency:** retry loops sleep even on the final attempt —
  16 s waste per failed stage at defaults (`plan.py:143-163`,
  `fix.py:161-167`, `visuals.py:89-100`, `_get` at `visuals.py:34-45`;
  `transcribe.py:121,160` already guards — copy that pattern).
- **Prompt/parsing brittleness:** fence stripping inconsistent across
  `plan.py:93-98`, `visuals.py:93`, `fix.py:164` (leading prose burns a
  retry); full transcript embedded unbounded in the plan prompt
  (`plan.py:67-90` — fits 20-min videos today); no dedicated `models.fix`
  knob (`fix.py:163` reuses `models.visuals`).
- **Help/text/UX:** `keys.txt` loads before arg parsing (even `--help`) and
  keeps junk `export K=V` lines (`cli.py:639-648,710-713`; gitignored +
  `setdefault` are correct); hardcoded `"50MB"` vs
  `telegram_send_max_bytes: 52428800` (`cli.py:465,472`); reproduce line
  unquoted (`report.py:101`, needs `shlex.quote`); preview `w//2:h//2` can go
  odd on odd configs → silent `None` (`report.py:43`); preview/sheet failures
  silent + oversized branch sends text-only with exit 0 (`report.py:25-60`,
  `cli.py:412-416,462-474` — the message itself is explicit);
  `attachments` accepts a file path then crashes on `glob/iterdir`, and
  `s_plan` offers dir names the fix loop later rejects (`cli.py:88-90,
  258-262` vs fix's `is_file()` filter); `Telegram()` clients never closed
  (`cli.py:456`, `acquire.py:40` — `close()` exists); `--name/--out`
  traversal (`cli.py:529`, `state.py:73-74` — operator input, harden anyway);
  fix rejection returns `0`, indistinguishable from pass to automation
  (`cli.py:581-585`, intentional + e2e-asserted — document it);
  `vedit qc` also rebuilds the report (`cli.py:606-613` — document or scope).
- **Schema hardening:** `Reframe.x_frac` unbounded (`schema.py:109-113`);
  `Transcript` allows `words==[]` (29-42); `Visual` bans single-word
  (`to>from`) while captions allow `to==from` (62-98); `EditPlan` checks
  segment *time* overlap only, not word-range overlap (132-144); reversed
  draft `10..5` silently widened to `10..11` instead of dropped
  (`plan.py:212-213`); `DraftVisual` has no kind/file coherence (by design —
  assembly repairs); `groq_chat` leaks `KeyError`/`IndexError` instead of
  `LlmError` on missing key/malformed-200 (`llm.py:52,84` — latent, callers
  check `missing_key` first); `shortlists` crashes on `{"icons": null}`
  (`visuals.py:62`).

---

## ✅ Checked and found correct

- `concat`/`concat_crossfade` A/V offset math (chained `xfade` offsets +
  same-window `acrossfade`; disabled → lossless `-c copy` concat). Current
  runs agree: `groq1`/`real1`/`mock1` duration drift 0.01–0.05 s (tolerance 0.25).
- `TimelineMap` round-trips/gap-collapse/boundaries (unit-tested).
- Segment fingerprint includes bounds/geometry/zoom/overlays/stats/`cfg.hash`
  + sidecar gating; parallel encode index-preserving on distinct files.
- `correct_words`/`_merge_tiny`/silence-detect assist per unit tests.
- SRT path (`text_plain`, no ASS tags); `_srt_time` total-ms `divmod`.
- Acquire happy paths + `max_input_s`/stream checks; Telegram download caps +
  partial cleanup; local same-file skip.
- Fix bounds + `_same_segment` + post-patch re-validation; `replace_icon`
  blocked when icons disabled; `unknown` mutates nothing; repick fallback.
- `resolve_visuals` per-asset `try` (one bad asset never kills render).
- QC hard/warn split matches design; all deliver callers gate on `qc.passed`;
  `cmd_deliver` requires `chat_id`.
- Marker resume design (`run_stage` marks only on success); `keys.txt`
  gitignored + `setdefault` doesn't clobber env; manifest save-side
  `is_relative_to` filter.

---

## 🧪 Test gaps (no coverage today)

- No `tests/unit/test_qc.py` at all — H2's vacuous pass is untested.
- No burn-overwrite test — e2e fix tests (`test_fix_patches_without_replan`,
  `test_fix_can_place_input_image`) re-render and assert QC pass, which also
  passes on the stale file: H3 is invisible to the suite.
- `_ass_time` minute-boundary (L1); overlay `fade st` (M14);
  `prepare`/`ensure_base` staleness (H1); `_load_resolved` traversal (H5);
  `attach` parsing/ext (M6); single-word boundary visuals (M7);
  coverage-gap degrade (M13); `apply_limits` out-of-range (M9); transcribe
  negative/NaN + Gemini-block concat (M10/M11); retry-sleep waste.

---

## 🛠 Recommended fix waves

1. **Delivery integrity** — H3 (`-y` + codec pins + burn-overwrite test),
   H2 (QC schema validation + `test_qc.py`), H1 run-param/config comparison,
   M1 (`_deliver` self-gate), M5 (atomic saves).
2. **Timeline integrity** — H4 (shared overlap helper used by render + QC +
   captions, incl. shrink rule), M13 (coverage → early `degraded`), M7
   (backward widen), caption LOWs (carry/escaping/width).
3. **Trust boundary** — H5/H7/M2/M3 (manifest + pick + resolve validation),
   H6 (token redaction).
4. **Robustness** — cache fingerprinting (H1 facets), M4/M6/M8–M12/M14–M16,
   remaining LOWs.
