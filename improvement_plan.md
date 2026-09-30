# vedit — Codebase Analysis & Implementation Plan

## Part 1: General Improvement Opportunities

### 🔴 High Priority

| # | Area | Issue | Where |
|---|------|-------|-------|
| 1 | **Jump-cut glitch** | Segments are hard-cut and concat'd with `-c copy`. The 0.01s `afade` micro-fades at segment boundaries are too short to mask the visual pop — they only prevent audio clicks, not the perceived "glitch". No video crossfade or transition exists between segments. | [render.py:237-239](vedit/stage/render.py#L237-L239) |
| 2 | **No bass boost** | The audio chain is `highpass→afftdn→acompressor→loudnorm`. There's no low-shelf / bass EQ stage, and `highpass=80` actually *removes* bass below 80 Hz. No config knob for bass. | [audio.py:32-39](vedit/stage/audio.py#L32-L39), [config.yaml:24-33](config.yaml#L24-L33) |
| 3 | **Audio extracted as stereo** | `prepare()` extracts at `-ac 2` (stereo), but speech-centric edits benefit from mono processing. The STT path already downmixes to mono. Stereo masking can hide noise that mono denoising would catch. | [audio.py:86](vedit/stage/audio.py#L86) |

### 🟡 Medium Priority

| # | Area | Issue | Where |
|---|------|-------|-------|
| 4 | **No segment caching** | `render_segments` always re-encodes every segment — even on a fix run where only one visual changed. The old segments are deleted unconditionally (`out.unlink()`). Only the base reframe is cached. | [render.py:273-274](vedit/stage/render.py#L273-L274) |
| 5 | **Concat list uses POSIX paths** | `concat()` writes `.as_posix()` paths into the concat list. On Windows this works if the drive letter resolves, but it's fragile — ffmpeg on Windows can choke on forward-slash absolute paths. | [render.py:325](vedit/stage/render.py#L325) |
| 6 | **`_merge_tiny` can re-admit pauses** | The epsilon guard (`1e-6`) was added to fix a float-boundary flake, but the merge is greedy-left — a short segment between two long ones always merges into the *left* neighbour, potentially re-spanning a pause the cut just removed. | [cuts.py:94-115](vedit/stage/cuts.py#L94-L115) |
| 7 | **Unused `vlabel` in render** | `vlabel = "[v0]"` is assigned but overwritten by the chain logic; the `_ = vlabel` suppression at line 241 is a dead-code smell. | [render.py:185, 241](vedit/stage/render.py#L185) |
| 8 | **No parallel segment encode** | `render_segments` is sequential. The implementation.md mentions `xargs -P 2` parallelism as possible, but it was never implemented. For ≥10 segments this doubles render time. | [render.py:259-316](vedit/stage/render.py#L259-L316) |

### 🟢 Low Priority / Polish

| # | Area | Issue | Where |
|---|------|-------|-------|
| 9 | **`IconPicks` forward-ref** | `IconPicks` references `IconPick` before its definition (line 198 vs 201). This works at runtime due to pydantic's deferred resolution, but it's a code-smell and breaks some static analysis. | [schema.py:198-204](vedit/schema.py#L198-L204) |
| 10 | **Hardcoded margins in `_place`** | Magic numbers `300`, `420`, `240`, `200` for overlay Y placement. Should be config knobs or at least named constants. | [render.py:90-101](vedit/stage/render.py#L90-L101) |
| 11 | **Missing type annotation** | `_measure` returns `dict` but it's really a `dict[str, str]` with specific keys. A `TypedDict` would catch key typos. | [audio.py:42](vedit/stage/audio.py#L42) |
| 12 | **No logging framework** | All logging is bare `print()` via `_log()`. No log levels, no structured output, hard to filter in CI. | [cli.py:37-38](vedit/cli.py#L37-L38) |

---

## Part 2: Jump-Cut Glitch — Root Cause & Fix

### Root Cause

The "glitchy" jump cuts come from **three compounding issues**:

1. **No video transition between segments.** `concat -c copy` is a hard byte splice — there is zero visual blending between segments. The viewer sees an instantaneous frame jump.

2. **Ultra-short audio fades.** The `afade=t=in:d=0.01` / `afade=t=out:d=0.01` (10 ms) exist only to prevent codec-level pops. They do nothing for perceived smoothness.

3. **Cut boundaries land mid-syllable.** `pad_ms=120` is applied to the gap between words, but if whisper's word boundaries are slightly off, a cut can land on the tail of a plosive, creating an audible click even through the 10 ms fade.

### Proposed Fix: Segment-Boundary Crossfade

Instead of `-c copy` concat, encode a final pass that applies short video **crossfades** (configurable, default ~80–120 ms) and longer audio **crossfades** (~40–60 ms) at every segment join. This eliminates the visual pop and audible click in one step.

> [!IMPORTANT]
> This replaces the current lossless concat with one more encode pass. The tradeoff is ~10–15 seconds extra for a 60s video, but the quality improvement is massive for speech-editing use cases.

---

## Part 3: Bass Boost Preprocessing — Design

### Why it matters

The current chain *removes* bass (`highpass=80`) and then normalizes loudness. For voice content going to mobile speakers, a subtle low-shelf boost (around 200–300 Hz) adds warmth and perceived "weight" to the voice without muddying things.

### Where it fits

Insert **after** `highpass` and **before** `afftdn`:

```
highpass → bass_boost (new) → afftdn → acompressor → loudnorm
```

The bass boost is a **low-shelf EQ** (`equalizer` filter in ffmpeg) that lifts frequencies around a target center (default 250 Hz) by a configurable gain (default +4 dB). Placing it before denoising means any boosted noise is still cleaned up by `afftdn`.

---

## Part 4: Implementation Plan

> [!NOTE]
> All tuning knobs go into `config.yaml` per project convention. No hardcoded values in code.

### Phase 1: Bass Boost Preprocessing

**Files touched:** [config.yaml](config.yaml), [config.py](vedit/config.py), [audio.py](vedit/stage/audio.py)

| Step | Action | Detail |
|------|--------|--------|
| 1a | **Add config knobs** | Add to `config.yaml` under `audio:` section: `bass_boost_hz: 250` (center frequency), `bass_boost_gain_db: 4` (gain in dB), `bass_boost_width: 1.5` (bandwidth in octaves). `0` gain = disabled. |
| 1b | **Extend `AudioCfg`** | Add `bass_boost_hz: int`, `bass_boost_gain_db: float`, `bass_boost_width: float` fields to the frozen dataclass in `config.py`. |
| 1c | **Insert filter in chain** | In `_base_filters()`, inject `equalizer=f={hz}:t=h:w={width}:g={gain}` between `highpass` and `afftdn` when `bass_boost_gain_db != 0`. The `t=h` flag means "half-cosine" shelf which is smooth. |
| 1d | **Unit test** | Add `test_bass_boost_filter_string` in `tests/unit/` that checks the filter chain includes the equalizer when gain > 0 and omits it when gain == 0. |
| 1e | **Clean cache** | Since the filter chain changed, `audio.py:prepare()` already skips if `clean_48k.wav` exists. Users re-running need to delete `work/audio/` or use `--from-stage audio`. Document this. |

```diff
 # config.yaml, audio section
  audio:
    highpass_hz: 80
+   bass_boost_hz: 250
+   bass_boost_gain_db: 4
+   bass_boost_width: 1.5
    afftdn_nf: -30
```

```diff
 # audio.py, _base_filters()
  def _base_filters(cfg: Config) -> str:
      a = cfg.audio
-     return (
-         f"highpass=f={a.highpass_hz},"
-         f"afftdn=nf={a.afftdn_nf},"
+     parts = [f"highpass=f={a.highpass_hz}"]
+     if a.bass_boost_gain_db:
+         parts.append(
+             f"equalizer=f={a.bass_boost_hz}:t=h:w={a.bass_boost_width}:g={a.bass_boost_gain_db}"
+         )
+     parts.append(f"afftdn=nf={a.afftdn_nf}")
+     parts.append(
          f"acompressor=threshold={a.comp_threshold_db}dB:ratio={a.comp_ratio}:"
          f"attack={a.comp_attack_ms}:release={a.comp_release_ms}"
      )
+     return ",".join(parts)
```

### Phase 2: Smooth Jump Cuts (Crossfade Concat)

**Files touched:** [config.yaml](config.yaml), [config.py](vedit/config.py), [render.py](vedit/stage/render.py), [qc.py](vedit/stage/qc.py)

| Step | Action | Detail |
|------|--------|--------|
| 2a | **Add config knobs** | `cuts.crossfade_video_ms: 100` (video crossfade at every segment join, 0 = hard cut), `cuts.crossfade_audio_ms: 50` (audio crossfade). |
| 2b | **Extend `CutsCfg`** | Add `crossfade_video_ms: int = 0`, `crossfade_audio_ms: int = 0`. |
| 2c | **Extend segment tail/head** | In `render_segments`, when crossfades are enabled, extend each segment's encode window by `crossfade_ms / 2` on both sides (clamped to source bounds). This provides the overlap frames needed for the blend. |
| 2d | **Replace `concat -c copy`** | Replace the lossless concat with a `filter_complex` that applies `xfade` (video) and `acrossfade` (audio) between consecutive segment pairs. For N segments this is N-1 xfade chains. The xfade filter: `xfade=transition=fade:duration=0.1:offset=<T>` where T is the cumulative output time minus the overlap. |
| 2e | **Adjust QC duration check** | Each crossfade shortens the output by `crossfade_ms`. Update the expected duration calculation in `qc.py` to account for `(N-1) * crossfade_ms`. |
| 2f | **Fallback** | If crossfade is `0` (disabled), keep the old `-c copy` concat as-is. Zero behavior change for users who don't opt in. |
| 2g | **Test** | Add `test_crossfade_concat` that verifies ffprobe output duration is correct with 2–3 synthetic segments. |

```diff
 # config.yaml, cuts section
  cuts:
    cut_gap_ms: 600
    keep_gap_ms: 350
    pad_ms: 120
+   crossfade_video_ms: 100
+   crossfade_audio_ms: 50
```

The new concat function (simplified):

```python
def concat_crossfade(paths: list[Path], workdir: Path, cfg: Config) -> Path:
    """Crossfade-concat N segments using xfade + acrossfade filters."""
    if len(paths) < 2 or cfg.cuts.crossfade_video_ms == 0:
        return concat(paths, workdir)  # fallback to lossless

    xf_s = cfg.cuts.crossfade_video_ms / 1000.0
    axf_s = cfg.cuts.crossfade_audio_ms / 1000.0

    inputs = []
    for p in paths:
        inputs += ["-i", str(p)]

    # Build xfade chain: [0:v][1:v]xfade=...[v01]; [v01][2:v]xfade=...[v012]; ...
    # Build acrossfade chain similarly for audio
    v_chain, a_chain = [], []
    offsets = []
    cumulative = 0.0
    for i, p in enumerate(paths):
        dur = _probe_duration(p)
        if i > 0:
            offset = cumulative - xf_s
            offsets.append(offset)
            prev_v = f"[v{i-1}]" if i > 1 else "[0:v]"
            v_chain.append(
                f"{prev_v}[{i}:v]xfade=transition=fade:duration={xf_s}:offset={offset}[v{i}]"
            )
            prev_a = f"[a{i-1}]" if i > 1 else "[0:a]"
            a_chain.append(
                f"{prev_a}[{i}:a]acrossfade=d={axf_s}:c1=tri:c2=tri[a{i}]"
            )
        cumulative += dur - (xf_s if i > 0 else 0)

    graph = ";".join(v_chain + a_chain)
    out = workdir / "concat.mp4"
    ff.ffmpeg(inputs + ["-filter_complex", graph, ...])
    return out
```

### Phase 3: Quick Wins (Optional, After 1+2)

| Step | Action | Files |
|------|--------|-------|
| 3a | Fix `IconPicks`/`IconPick` forward-ref ordering | [schema.py](vedit/schema.py#L197-L204) |
| 3b | Remove dead `vlabel` assignment | [render.py](vedit/stage/render.py#L185) |
| 3c | Add parallel segment encode with `concurrent.futures.ThreadPoolExecutor` | [render.py](vedit/stage/render.py#L259) |
| 3d | Add segment-level cache (hash overlays+zoom+bounds → skip re-encode) | [render.py](vedit/stage/render.py#L273) |

### Execution Order & Dependencies

```mermaid
graph LR
    A["Phase 1: Bass Boost<br/>~1 hour"] --> C["Phase 3: Quick Wins<br/>~1 hour"]
    B["Phase 2: Crossfade<br/>~2–3 hours"] --> C
    A -.-> B
    style A fill:#2d5a27,stroke:#4CAF50,color:#fff
    style B fill:#8B4513,stroke:#D2691E,color:#fff
    style C fill:#1a3a5c,stroke:#4a9eff,color:#fff
```

> [!TIP]
> Phase 1 (bass boost) is independent and safe — it only touches the audio filter chain. Ship it first, test with a real recording, then tackle Phase 2 (crossfade) which is more invasive since it changes the concat strategy.

### Checklist Before Commit

- [ ] `ruff check` passes
- [ ] `ruff format --check` passes
- [ ] `pytest` passes (all new + existing tests)
- [ ] Manual test: process a real recording with bass boost ON, verify warmth
- [ ] Manual test: process with crossfade ON, verify no visual pop at cuts
- [ ] `config.yaml` documents every new knob with inline comments
