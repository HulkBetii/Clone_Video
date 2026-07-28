# Project Log

## Project Direction

- Start with backend development.
- Defer UI implementation until the backend foundation is stable.
- Keep this file updated after each meaningful project change.

## Current Status

- Phase: Local web UI and auto-GPT workspace pipeline implemented
- Backend: YouTube transcript and Playwright GPT rewrite APIs implemented; real-profile full rewrite completed against the current ChatGPT UI
- Frontend/UI: React/Vite SPA implemented and served by FastAPI; desktop/mobile E2E and browser QA passed
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
- Added current ChatGPT attachment, fallback-composer, and assistant-citation DOM compatibility.
- Added workspace coordinator APIs that chain completed transcript jobs into rewrite jobs, survive closed tabs/restarts, and map recoverable GPT failures to `waiting_for_user`.
- Added GPT runtime controls for opening, checking, and closing the headed `PROFILE_GPT_1` browser without importing credentials.
- Added the React/TypeScript/Vite local SPA with responsive create, library, workspace, and system routes; artifacts load lazily and the UI remains read-only in v1.
- Added static SPA fallback serving from FastAPI and generated production assets under `src/yt_pro_max/static`.
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
- [x] Implement local web UI and workspace orchestration

## Next Step

Keep the backend/UI release stable while adding future workspace stages. Whisper CUDA now uses the configured local DLL directory; real GPT smoke remains opt-in.

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

### 2026-07-27 - Real ChatGPT profile smoke

- Used transcript job `148de373-0b51-4740-8678-649111846678` for video `HJHPkBYoo9I` with the real `PROFILE_GPT_1` session.
- Confirmed the profile is authenticated, the Japanese transcript uploads, and ChatGPT returns valid JSON identifying `language=ja` and the `Title:` header.
- Successful smoke conversation: `https://chatgpt.com/c/6a676954-cc90-83ec-a973-74db7d177e1c`.
- Full rewrite smoke failed before analysis because the current ChatGPT UI marks processing attachments with `cursor-wait`; the adapter returned from upload early while `send-button` was still disabled.
- Later retries attempted the same attachment again and surfaced `GPT_UPLOAD_FAILED`, masking the initial send-readiness failure.

### 2026-07-27 - ChatGPT readiness fix and full rewrite smoke

- Updated attachment detection to inspect visible composer file tiles, wait for `cursor-wait`/busy/progress descendants, and accept ChatGPT's ready-state filename format `<stem>(n)<suffix>` without matching remove buttons.
- Added the navigation fallback composer `textarea[name="prompt-textarea"]` used before ChatGPT upgrades the editor to `#prompt-textarea`.
- Removed interactive attachment citation buttons from cloned assistant DOM before response parsing so citation chips cannot corrupt structured JSON.
- Completed rewrite job `smoke-full-final3-148de373-20260727` for source job `148de373-0b51-4740-8678-649111846678` and video `HJHPkBYoo9I`.
- Generated title: `左手の薬指が人差し指より長い人に見られる4つの特徴｜魅力・直感・挑戦心・創造性`.
- Published `data/rewrite_jobs/smoke-full-final3-148de373-20260727/HJHPkBYoo9I.ja.rewrite.txt`; source/output lengths are 3,408/3,932 characters, a 115.38% ratio.
- Writer conversation: `https://chatgpt.com/c/6a67741e-12dc-83ec-9ce0-d4a8c06282c6`; validator conversation: `https://chatgpt.com/c/6a677bf0-4ba8-83ec-aa7a-69b7694c0367`.
- A transient browser crash during targeted validator repair was recovered from the persisted checkpoint; automatic retryable-job requeue remains a reliability follow-up.
- Verification: `83 passed, 2 skipped`; Ruff check/format, lockfile, UTF-8/title/artifact, and diff checks passed.

### 2026-07-27 - Local web UI and workspace workflow

- Added `POST /api/v1/workspaces`, workspace listing/detail/resume endpoints, startup recovery, and transcript-to-rewrite coordination.
- Added `GET/POST /api/v1/gpt-runtime` controls for the shared headed Chromium profile, with busy guards and manual-login recovery.
- Added React Router, TanStack Query, Radix Tabs/Tooltip, Lucide, local Newsreader/IBM Plex Sans fonts, and responsive editorial-control-room styling.
- Added lazy transcript/rewrite artifact preview, TTS body copy, downloads, comparison metrics, validation issues, status filters, and responsive mobile navigation.
- Smoke-checked the real workspace `148de373-0b51-4740-8678-649111846678` and confirmed `PROFILE_GPT_1` returns `ready` and `authenticated` before releasing the profile lock.
- Verification: Python `102 passed, 2 skipped`; Ruff passed; frontend lint/typecheck/Vitest `9 passed`; Playwright E2E `3 passed`; production build passed; local browser QA passed at desktop and 390px mobile viewport.

### 2026-07-28 - Whisper CUDA runtime fix

- Added `YT_PRO_MAX_CUDA_DLL_DIR` and Windows DLL bootstrap that prepends the configured directory to process `PATH` while retaining the `os.add_dll_directory` handle required by CTranslate2 and health probes.
- Configured the local backend to reuse the compatible CUDA 12/cuDNN 9 DLLs already installed with Torch, without copying binaries into the repository.
- Changed the System page from the misleading `NOT_LOADED` state to `CUDA sẵn sàng` with an explicit lazy-load message; CPU fallback remains visible when CUDA userspace is unavailable.
- Ran a real cached `tiny` model inference on RTX 3060 using CUDA `float16`; result: `CUDA_INFERENCE_OK`.
- Verification: health status `ok`, CUDA device count `1`, runtime available `true`; Python `103 passed, 2 skipped`; frontend Vitest `10 passed`; lint/typecheck/build and Playwright E2E `3 passed`.

### 2026-07-28 - Library workspace deletion

- Added per-workspace checkboxes, select-all/deselect-all controls, a selected-count delete action, and an explicit confirmation panel on the Library page.
- Added `POST /api/v1/workspaces/bulk-delete` to remove selected transcript jobs, rewrite jobs, artifacts, and staging directories in one safe operation.
- Active queued/running transcript or rewrite jobs are protected and return `409` instead of being deleted.
- Browser QA selected one completed workspace, opened the confirmation panel, canceled the action, and verified all four real workspaces remained.
- Verification: Python `105 passed, 2 skipped`; Ruff passed; frontend lint/typecheck/Vitest `11 passed`; production build and Playwright E2E `3 passed`.

### 2026-07-28 - Long Japanese rewrite smoke

- Tested `https://www.youtube.com/watch?v=bgSqJPlQbSM` as a full Transcript + GPT workspace (`6801857c-060f-4261-84cb-23b8d1d957e7`).
- Transcript completed from automatic Japanese captions: duration `1,896` seconds and normalized body `10,868` characters, so the `25,000`-character policy selected one semantic section (`1/1`), not long-form chunking.
- GPT completed source analysis, outline, one-section rewrite, and editing. The edited staging draft is `10,728` characters (`98.71%` of source), below the required `100-130%` acceptance range and would require a targeted length repair.
- Independent validation crashed three times with `GPT_BROWSER_CRASHED` after browser restart/resume; the job remains `waiting_for_user` with `GPT_BROWSER_CRASHED`, and no final TXT artifact was published.

### 2026-07-28 - Rewrite validation payload/recovery fix

- Changed edit, validation, and targeted repair prompts to reference staged attachments instead of embedding the same long source/draft text twice in the ChatGPT composer.
- Validation and repair retries now start fresh ChatGPT conversations instead of recovering stale request markers; completed `repaired-###.txt` files are loaded on resume.
- Added explicit length repair instructions and a guard that preserves a length-valid draft when a GPT repair candidate would become too short or too long.
- Added regression tests for attachment-only prompts, stale conversation recovery, exact length repairs, shrinking candidates, and persisted repair resume.
- Verification: Python `110 passed, 2 skipped`; Ruff passed; diff check passed.
- Real smoke after the runtime fix no longer reproduced `GPT_BROWSER_CRASHED`, but the persisted job still ended safely without an artifact when GPT returned invalid/failed repair output; the remaining issue is model-output quality, not browser recovery.

### 2026-07-28 - Long rewrite validator contract fix

- Tightened the validator prompt to require JSON booleans for `passed`, `language_match`, and `tts_ready`, numeric scores, and arrays for issue fields.
- Made boolean parsing tolerate strict scalar variants commonly emitted by multilingual GPT responses while rejecting ambiguous prose; parser errors now identify the invalid field and type.
- Resumed rewrite job `fe54afde-aebb-4790-aedf-c16e1a6b04c4` for workspace `6801857c-060f-4261-84cb-23b8d1d957e7` without clearing persisted analysis, outline, section, edit, or repair checkpoints.
- Published `data/rewrite_jobs/fe54afde-aebb-4790-aedf-c16e1a6b04c4/bgSqJPlQbSM.ja.rewrite.txt` with UTF-8 Japanese output: source/output lengths `10,301/11,121` normalized characters (`107.96%`).
- Final GPT validation passed: style `97`, coverage `99`, language match `true`, TTS ready `true`, no unsupported claims, and no missing points. Local validation also passed with no warnings.
- Verification: rewrite content/pipeline tests `32 passed`; full Python suite `111 passed, 2 skipped`; Ruff and diff checks passed.

### 2026-07-28 - Japanese audio-first transcript

- Japanese automatic captions now bypass caption rendering and use downloaded audio with Whisper `turbo`, forced `ja`, VAD, word timestamps, and an independent language-detection sample.
- Manual Japanese captions remain preferred; automatic captions for non-Japanese languages keep the existing caption-first behavior.
- Whisper download/model/transcription/language failures are classified and do not fall back to the Japanese auto-caption track.
- Bumped transcript pipeline cache version from `1` to `2` so old automatic-caption artifacts are not reused.
- Added regression coverage for `ja`, `ja-JP`, `ja-orig`, manual Japanese, non-Japanese automatic captions, language mismatch, failure safety, JSON word metadata, progress stages, language forwarding, and model import errors.
- Real smoke `fa65a160-691f-4e90-880f-d361a79bb757` for `bgSqJPlQbSM` completed with `source=whisper`, `language=ja`, confidence `0.9990234375`, `667` segments, word timestamps on all segments, and warning `JAPANESE_AUTO_CAPTION_REPLACED_BY_WHISPER`.
- The audio-first artifact is `data/jobs/fa65a160-691f-4e90-880f-d361a79bb757/【美輪明宏】夜中の同じ時間に目覚める人、実は〇〇なのよ。誰も言わなかった本当の理由。_偉人_名言_言葉の力_人生哲学_.bgSqJPlQbSM.ja.json`; audio-first improves timing/source grounding but does not correct every Japanese homophone or proper noun automatically.
- Verification: full Python suite `123 passed, 2 skipped`; Ruff and diff checks passed.
