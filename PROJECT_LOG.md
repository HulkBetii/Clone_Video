# Project Log

## Project Direction

- Start with backend development.
- Defer UI implementation until the backend foundation is stable.
- Keep this file updated after each meaningful project change.

## Current Status

- Phase: Backend v2 rewrite pipeline implemented
- Backend: YouTube transcript and Playwright GPT rewrite APIs implemented and verified; live GPT smoke remains opt-in
- Frontend/UI: Deferred
- Skills: Not installed yet
- Repository: Python 3.11 FastAPI project with SQLite job persistence

## Completed

- Created the project progress log.
- Decided to follow a backend-first development sequence.
- Defined the YouTube transcript backend scope and processing policy.
- Added asynchronous job API, SQLite persistence, cache, and restart recovery.
- Added caption-first extraction with local Whisper fallback.
- Added SRT, TXT, and JSON artifact rendering.
- Added rolling-caption deduplication for YouTube auto-caption VTT files.
- Added video titles to artifact filenames and SRT/TXT content.
- Added asynchronous GPT rewrite jobs with adaptive chunking and TTS-ready TXT output.
- Added unit/integration tests and an opt-in live YouTube smoke test.

## Decisions

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-07-27 | Prioritize backend before UI | Establish the core functionality and data flow first. |
| 2026-07-27 | Track progress in `PROJECT_LOG.md` | Preserve project context across development sessions. |
| 2026-07-27 | Use Python 3.11, FastAPI, SQLite, and uv | Match the local ML/media tooling and keep the backend easy to run locally. |
| 2026-07-27 | Prefer manual captions, then auto captions, then local Whisper | Minimize processing cost while supporting videos without captions. |
| 2026-07-27 | Support public/unlisted completed videos only | Avoid bypassing access controls and realtime livestream complexity. |
| 2026-07-27 | Keep rewrite jobs separate from transcript jobs | Allow independent cache, retry, restart recovery, and GPT failure handling. |
| 2026-07-27 | Use the existing auto_YT GPT profile without fallback | Reuse the authenticated local browser while preventing silent account/profile changes. |

## Milestones

- [x] Define backend requirements and scope
- [x] Choose backend stack and supporting services
- [x] Design data model and API contract
- [x] Implement backend foundation
- [x] Add automated tests
- [x] Validate backend integration points
- [x] Implement Playwright GPT rewrite backend
- [ ] Revisit UI requirements

## Next Step

Stop other processes using `PROFILE_GPT_1`, then run the opt-in ChatGPT attachment smoke test. CUDA 12 cuBLAS/cuDNN installation remains optional for GPU Whisper.

## Change History

### 2026-07-27

- Initialized the project log.
- Recorded the backend-first direction.
- Deferred UI work.

### 2026-07-27 - Backend v1

- Added `pyproject.toml`, `uv.lock`, Python 3.11 configuration, and environment settings.
- Implemented YouTube URL validation, metadata/access checks, caption selection, and audio download through `yt-dlp`.
- Implemented local `faster-whisper` transcription with CUDA-to-CPU fallback.
- Implemented SQLite-backed asynchronous jobs, cache reuse, force refresh, and restart recovery.
- Implemented FastAPI endpoints for job creation, polling, artifacts, and health.
- Added SRT/TXT/JSON rendering with normalized timestamps and UTF-8 output.
- Verification: `29 passed, 1 skipped`; live public YouTube metadata and caption smoke checks passed.
- Real Whisper smoke with the `tiny` model succeeded on CPU fallback; CUDA reported `cublas64_12.dll` missing and health now reports `degraded`.

### 2026-07-27 - Rolling caption normalization

- Replaced `webvtt-py` parsing with a direct VTT cue parser to preserve inline-timestamp cues.
- Collapsed rolling windows by carrying forward existing lines and emitting only newly introduced text.
- Added regression coverage for rolling captions, leading blank cue lines, and legitimate repeated text after a gap.
- Retested video `HJHPkBYoo9I`: output reduced from 368 to 185 segments with no adjacent duplicate text or overlapping timestamps.
- Verification: `31 passed, 1 skipped`; lint and lockfile checks passed.

### 2026-07-27 - Video title artifacts

- Added a normalized `Title:` header to TXT output and a separate SRT header before cue 1.
- Added sanitized Unicode video titles to SRT, TXT, and JSON filenames while retaining the video ID and language.
- Protected Windows filenames from invalid characters, reserved names, trailing dots/spaces, and excessive title length.
- Retested video `HJHPkBYoo9I`; all artifacts include the title and the API job completed successfully.
- Verification: `31 passed, 1 skipped`; lint and lockfile checks passed.

### 2026-07-27 - GPT video rewrite backend

- Added separate SQLite-backed rewrite jobs, API polling, cache reuse, force refresh, artifact download, and restart checkpoints.
- Added a dedicated single-concurrency async Playwright worker using the existing `auto_YT` persistent ChatGPT profile.
- Added resilient file upload, login/profile-lock checks, stable response extraction, conversation restore, request deduplication, and classified retries.
- Added source isolation prompts, global style analysis, exact-coverage outlines, adaptive semantic chunks, section rewriting, editorial passes, and independent GPT validation.
- Added persisted seam editing so adjacent long-form sections receive a bounded transition pass before independent validation.
- Enforced validator language/unsupported-claim/missing-point fields, immediate conversation checkpoint persistence, and stable Playwright request recovery without accepting transient placeholders.
- Scoped rewrite cache to source content and pipeline/profile/length policy, independent of transcript job ID.
- Made rewrite shutdown cancellation-safe, preserved unfinished jobs for restart recovery, and kept the worker alive after isolated failures.
- Standardized immediate rewrite API failures on the shared error contract.
- Enforced one SEO title, source-language output, 100-130% normalized length, sponsor removal, generic CTA retention, and ElevenLabs/Minimax-ready plain text.
- Added deterministic validation, atomic TXT rendering, short/long integration coverage, and an opt-in live ChatGPT attachment smoke test.
- Verification: `80 passed, 2 skipped`; Ruff check/format, lockfile, and diff checks passed.
