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

## Processing policy

- Manual captions are preferred over automatic captions.
- A requested language must have a matching caption track; the backend does not silently translate or switch languages.
- Without a requested language, missing captions trigger local Whisper transcription.
- Public and unlisted videos are supported. Private, members-only, restricted, unavailable, playlist-only, upcoming, and active livestream URLs return classified errors.
- The default duration limit is six hours and can be changed with `YT_PRO_MAX_MAX_DURATION_SECONDS`.
- Completed artifacts are cached by video, language, pipeline version, and Whisper profile. Use `force_refresh` to reprocess.

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
