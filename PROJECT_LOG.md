# Project Log

## Project Direction

- Start with backend development.
- Defer UI implementation until the backend foundation is stable.
- Keep this file updated after each meaningful project change.

## Current Status

- Phase: Backend v1 implemented
- Backend: YouTube transcript API implemented and verified; health is degraded until CUDA 12 runtime libraries are installed
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
- Added unit/integration tests and an opt-in live YouTube smoke test.

## Decisions

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-07-27 | Prioritize backend before UI | Establish the core functionality and data flow first. |
| 2026-07-27 | Track progress in `PROJECT_LOG.md` | Preserve project context across development sessions. |
| 2026-07-27 | Use Python 3.11, FastAPI, SQLite, and uv | Match the local ML/media tooling and keep the backend easy to run locally. |
| 2026-07-27 | Prefer manual captions, then auto captions, then local Whisper | Minimize processing cost while supporting videos without captions. |
| 2026-07-27 | Support public/unlisted completed videos only | Avoid bypassing access controls and realtime livestream complexity. |

## Milestones

- [x] Define backend requirements and scope
- [x] Choose backend stack and supporting services
- [x] Design data model and API contract
- [x] Implement backend foundation
- [x] Add automated tests
- [x] Validate backend integration points
- [ ] Revisit UI requirements

## Next Step

Install or locate CUDA 12 cuBLAS and cuDNN 9 if GPU Whisper is required, then run a real `turbo` no-caption transcription; CPU fallback is currently working.

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
