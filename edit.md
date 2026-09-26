# AI Video Editing Agent — Chat Compaction

## Context

I am starting a personal educational video series where I articulate my thoughts and explain basic software/CS concepts such as:

- APIs
- JWT
- Authentication
- Other foundational software engineering terms

The goal is to publish useful educational videos without spending significant time manually editing them.

I am a student, so I want to minimize editing time. I am willing to spend time **reviewing the finished video**, but I do not want to manually perform the entire editing workflow.

## Core Idea

Build a personal-use **AI video editing agent**.

Workflow:

> Record video → Push/upload video to agent → Agent automatically edits it → I review → Publish

The system should handle repetitive editing automatically while leaving final quality control to me.

## Desired Automated Editing

### 1. Silence Removal

Automatically detect and remove:

- Long pauses
- Dead air
- Unnecessary gaps

The result should feel like natural jump cuts rather than aggressively chopping speech.

### 2. Voice Enhancement

Automatically improve the recorded audio:

- Noise reduction
- Voice clarity
- Loudness normalization
- Consistent volume

The objective is clean, understandable educational speech.

### 3. Relevant Visuals

While I explain a concept, the agent should identify relevant concepts/keywords and place useful visuals over the video.

Examples:

- Saying **API** → API-related diagram/visual
- Saying **JWT** → JWT/token visual
- Saying **authentication** → lock/authentication graphic
- Explaining a technical process → relevant diagram or animation

Visuals should appear at appropriate timestamps and disappear when no longer useful.

### 4. Pop-up Images / Animations

The agent should be able to add lightweight visual elements such as:

- Icons
- Images
- Diagrams
- Text callouts
- Simple animations
- Pop-up explanations
- Zooms/highlights where appropriate

The editing style should support educational short-form/long-form content rather than looking like an over-edited meme.

### 5. Subtitles

Automatically generate captions from the transcript.

Potential style:

- Clean captions
- Highlighted keywords
- Word/phrase emphasis where appropriate

### 6. Automatic Jump Cuts / Zooms

The agent can optionally introduce subtle editing effects:

- Jump cuts after silence removal
- Slight zoom-ins
- Occasional framing changes
- Emphasis around important statements

These should be controlled and not excessive.

## Important Design Principle

Do **not** try to build a completely unconstrained autonomous video editor.

The better approach is:

> **AI decides what should happen + deterministic templates/rules decide how it is rendered.**

This makes the output predictable and easier to improve.

For example:

```text
Transcript
   ↓
Concept / keyword extraction
   ↓
Timestamp identification
   ↓
Visual selection
   ↓
Editing decisions
   ↓
FFmpeg / rendering pipeline
   ↓
Final video
```

## Visual Strategy

A controlled visual library can be used instead of relying entirely on random stock-image searches.

Example mapping:

```json
{
  "API": "api_animation.mp4",
  "JWT": "jwt_token.png",
  "authentication": "authentication_lock.png",
  "database": "database_animation.mp4",
  "server": "server_icon.png"
}
```

The system can later support dynamically generated/search-based visuals.

## Proposed Pipeline

```text
[Raw Video]
     |
     v
[Audio Extraction]
     |
     +--> [Voice Enhancement]
     |
     v
[Speech-to-Text / Transcript]
     |
     v
[Silence Detection]
     |
     v
[Concept + Keyword Extraction]
     |
     v
[Timestamped Editing Plan]
     |
     +--> [Visual Selection]
     |
     +--> [Subtitle Generation]
     |
     +--> [Zoom / Emphasis Decisions]
     |
     v
[FFmpeg Rendering]
     |
     v
[Final Edited Video]
     |
     v
[Human Review]
     |
     v
[Publish]
```

## Likely Technology Stack

### Video

- FFmpeg as the core rendering/editing engine

### Speech-to-Text

- Whisper or another speech-to-text model

### AI Reasoning

An LLM can:

- Understand the transcript
- Identify important concepts
- Decide where visuals are useful
- Generate an editing timeline/plan
- Generate captions/emphasis instructions

### Backend

Potentially:

- Python
- FastAPI

### Frontend

Only if needed:

- React
- Simple upload/review interface

For personal use, the first version can avoid building a complicated UI.

## MVP Scope

The first working version should NOT attempt everything.

### MVP v1

Input:

```text
Raw talking-head video
```

Output:

```text
Edited educational video
```

Automate:

1. Transcription
2. Silence removal
3. Audio enhancement
4. Subtitles
5. Keyword/concept extraction
6. Visual insertion from a predefined asset library
7. Basic zoom/emphasis
8. Final FFmpeg render

Then manually review the result.

## Quality Philosophy

The goal is **not perfect autonomous editing**.

The goal is:

> **One-click editing that gets the video 80–90% of the way there and saves hours of repetitive work.**

Human review remains part of the workflow.

## Future Improvements

Possible later features:

- Automatic B-roll search
- AI-generated diagrams
- AI-generated animations
- Automatic thumbnail generation
- Automatic title/description generation
- Automatic short/reel extraction
- Different editing presets
- Per-topic visual libraries
- Learning from my editing corrections
- Timeline preview before rendering
- Multiple output formats
- Automatic publishing workflow

## Example User Experience

I record:

> “An API is basically a way for two pieces of software to communicate with each other…”

I upload the raw video.

The agent:

1. Detects and removes pauses.
2. Cleans the audio.
3. Transcribes the speech.
4. Detects the concept **API**.
5. Places an API communication diagram on screen.
6. Adds a subtle animation.
7. Generates captions.
8. Highlights important terms.
9. Applies appropriate cuts/zooms.
10. Renders the final video.

I watch the result, make only necessary corrections, and publish.

## Core Objective

This is primarily a **personal productivity tool**, not initially a startup.

The main metric is:

> **How much editing time does this eliminate while maintaining acceptable video quality?**

If a manual editing workflow takes 1–3 hours, the target is to reduce my active editing time to roughly:

> **5–15 minutes of review/correction per video.**

## Key Constraint

I am a student with limited time.

Therefore:

- Prefer free/open-source tools where practical.
- Avoid unnecessary infrastructure.
- Keep the architecture simple.
- Automate repetitive work.
- Do not spend weeks building a polished editor UI.
- Optimize for actually using the system regularly.

## Product Definition

### Working description

> **A personal AI video editing agent for educational talking-head videos that automatically removes silence, enhances voice, generates captions, identifies concepts, adds relevant visuals/animations, and renders a publish-ready video for human review.**

### Guiding principle

> **Don't build an AI that replaces the editor. Build an agent that removes the boring parts of editing.**
