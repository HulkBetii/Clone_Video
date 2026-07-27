# YT Pro Max

Local backend for turning one completed YouTube video into timestamped transcript artifacts.

## Requirements

- Windows or Linux
- Python 3.11
- `uv`
- FFmpeg and FFprobe on `PATH`
- Node.js for the current YouTube JavaScript extraction runtime
- NVIDIA CUDA/cuDNN are optional; Whisper falls back to CPU when CUDA is unavailable

## Setup

```powershell
uv sync --dev --python 3.11
Copy-Item .env.example .env
```

The first Whisper job downloads the configured model from Hugging Face. Keep the backend local and do not expose it to untrusted clients in this version.

## Run

```powershell
uv run yt-pro-max
```

The API listens on `http://127.0.0.1:8000`.

The first GPT rewrite job requires a logged-in headed Chromium profile. Install the
Playwright browser once, then stop any other process using the shared profile before
starting a rewrite job:

```powershell
uv run playwright install chromium
```

## API

Create a job:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/transcript-jobs `
  -ContentType 'application/json' `
  -Body '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","language":"en"}'
```

Poll a job and download `srt`, `txt`, or `json` using the artifact URL returned by the job response:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/transcript-jobs/{job_id}
Invoke-WebRequest http://127.0.0.1:8000/api/v1/transcript-jobs/{job_id}/artifacts/srt -OutFile transcript.srt
```

Health checks are available at `GET /api/v1/health`.

Create a rewrite job from a completed transcript job:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/rewrite-jobs `
  -ContentType 'application/json' `
  -Body '{"transcript_job_id":"{transcript_job_id}"}'
```

Poll the rewrite job and download its TTS-ready TXT artifact:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/rewrite-jobs/{rewrite_job_id}
Invoke-WebRequest http://127.0.0.1:8000/api/v1/rewrite-jobs/{rewrite_job_id}/artifacts/txt -OutFile rewritten.txt
```

Rewrite output contains one `Title:` SEO header followed by plain UTF-8 script text.
Remove that first metadata line before sending the body to ElevenLabs or Minimax.

## Processing policy

- Manual captions are preferred over automatic captions.
- A requested language must have a matching caption track; the backend does not silently translate or switch languages.
- Without a requested language, missing captions trigger local Whisper transcription.
- Public and unlisted videos are supported. Private, members-only, restricted, unavailable, playlist-only, upcoming, and active livestream URLs return classified errors.
- The default duration limit is six hours and can be changed with `YT_PRO_MAX_MAX_DURATION_SECONDS`.
- Completed artifacts are cached by video, language, pipeline version, and Whisper profile. Use `force_refresh` to reprocess.
- Rewrite jobs accept only completed transcript job IDs, preserve the source language/style, target 110% length within 100–130%, remove sponsor-specific promotions, and keep generic CTAs.
- Long rewrites use semantic chunks, persisted section/seam checkpoints, one Playwright worker, independent GPT validation, and atomic TXT rendering.
- The rewrite worker uses `YT_PRO_MAX_GPT_PROFILE_DIR`, defaulting to `D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`; it never falls back to another profile.

## Tests

```powershell
uv run ruff check src tests
uv run pytest
```

The live YouTube smoke test is opt-in:

```powershell
$env:YT_PRO_MAX_SMOKE_URL = 'https://www.youtube.com/watch?v=...'
uv run pytest tests/test_smoke_youtube.py
```

The live ChatGPT attachment smoke test is also opt-in and requires the shared profile to be
logged in and unlocked:

```powershell
$env:YT_PRO_MAX_GPT_SMOKE = '1'
uv run pytest tests/test_smoke_rewrite_gpt.py
```
