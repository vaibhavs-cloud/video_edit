# Project notes

Cloudflare deploy account: Vaibhav personal — id `60d867f2eaa9a1dababb2d248d2768aa`, MCP server `cloudflare-personal` (pinned in `opencode.json`; matches implementation.md M3).

## Conventions

- All tuning knobs live in `config.yaml`, never in code.
- The LLM never emits cut timestamps; `edit_plan.json` uses word indices in source time.
- Active LLM provider is `config.yaml models.provider` (currently `groq`: whisper-large-v3 + gpt-oss-120b); Gemini remains a supported fallback behind `vedit/llm.py`.
- Run `ruff check`, `ruff format --check`, and `pytest` before committing (test.yml runs the same in CI).
