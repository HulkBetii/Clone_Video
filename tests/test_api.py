import time

from fastapi.testclient import TestClient

from yt_pro_max.app import create_app
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import TranscriptSource, VideoMetadata
from yt_pro_max.pipeline import PipelineOutput


class FakeTranscriber:
    def health(self):
        return {"loaded": False, "device": "not_loaded"}


class FakePipeline:
    def __init__(self, settings, *, fail=False):
        self.settings = settings
        self.transcriber = FakeTranscriber()
        self.fail = fail
        self.calls = 0

    def process(self, *, job_id, request_url, requested_language, update):
        self.calls += 1
        if self.fail:
            raise PipelineError("VIDEO_UNAVAILABLE", "Video is unavailable.")
        update("inspecting", 1)
        output_dir = self.settings.jobs_dir / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        files = {key: output_dir / f"video.vi.{key}" for key in ("srt", "txt", "json")}
        files["srt"].write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        files["txt"].write_text("Hello\n", encoding="utf-8")
        files["json"].write_text("{}\n", encoding="utf-8")
        return PipelineOutput(
            source=TranscriptSource.MANUAL_CAPTION,
            language="vi",
            language_confidence=None,
            video=VideoMetadata(
                id="dQw4w9WgXcQ",
                title="Demo",
                channel="Channel",
                duration_seconds=1,
                webpage_url=request_url,
            ),
            warnings=[],
            artifact_paths=files,
        )


def _wait_for_status(client, job_id, expected):
    for _ in range(100):
        response = client.get(f"/api/v1/transcript-jobs/{job_id}")
        if response.json()["status"] in expected:
            return response
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {expected}")


def test_create_poll_cache_and_download(settings):
    pipeline = FakePipeline(settings)
    app = create_app(settings, pipeline=pipeline)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/transcript-jobs",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "language": "vi"},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        completed = _wait_for_status(client, job_id, {"completed"})
        assert completed.json()["artifacts"]["srt"].endswith("/artifacts/srt")

        artifact = client.get(f"/api/v1/transcript-jobs/{job_id}/artifacts/srt")
        assert artifact.status_code == 200
        assert "Hello" in artifact.text

        cached = client.post(
            "/api/v1/transcript-jobs",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "language": "vi"},
        )
        assert cached.status_code == 200
        assert cached.json()["cached"] is True
        assert cached.json()["id"] == job_id
        assert pipeline.calls == 1

        refreshed = client.post(
            "/api/v1/transcript-jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "language": "vi",
                "force_refresh": True,
            },
        )
        assert refreshed.status_code == 202


def test_invalid_url_is_rejected_before_job_creation(settings):
    app = create_app(settings, pipeline=FakePipeline(settings))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcript-jobs",
            json={"url": "https://example.com/video"},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_YOUTUBE_URL"


def test_failed_job_exposes_sanitized_error(settings):
    app = create_app(settings, pipeline=FakePipeline(settings, fail=True))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/transcript-jobs",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        failed = _wait_for_status(client, created.json()["id"], {"failed"})
    assert failed.json()["error"] == {
        "code": "VIDEO_UNAVAILABLE",
        "message": "Video is unavailable.",
        "retryable": False,
        "details": {},
    }
