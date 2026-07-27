import asyncio
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from yt_pro_max.app import create_app
from yt_pro_max.errors import PipelineError
from yt_pro_max.models import JobStatus, RewriteStage, TranscriptSource
from yt_pro_max.repository import JobRepository
from yt_pro_max.rewrite_repository import RewriteJobRepository


class FakeTranscriber:
    def health(self):
        return {"loaded": False, "device": "not_loaded"}


class IdleTranscriptPipeline:
    transcriber = FakeTranscriber()

    def process(self, **kwargs):
        raise AssertionError("transcript pipeline should not run")


class FakeRewritePipeline:
    def __init__(self, settings, *, fail=None):
        self.settings = settings
        self.fail = fail
        self.calls = 0
        self.closed = False
        self.seen_checkpoints = []

    async def process(self, job, source_job, update):
        self.calls += 1
        self.seen_checkpoints.append(job.checkpoint)
        if self.fail:
            raise self.fail
        update(
            RewriteStage.REWRITING,
            60,
            1,
            2,
            checkpoint={"next_section": 2},
            conversation_url="https://chatgpt.com/c/test",
            work_files={"draft": "draft.txt"},
        )
        output_dir = self.settings.rewrite_jobs_dir / job.id
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = output_dir / "rewritten.txt"
        artifact_path.write_text("Title: SEO title\n\nRewritten content.\n", encoding="utf-8")
        return SimpleNamespace(
            artifact_path=artifact_path,
            title="SEO title",
            source_length=100,
            output_length=110,
            sections_total=2,
            sections_completed=2,
            warnings=[],
            conversation_url="https://chatgpt.com/c/test",
            checkpoint={"completed": True},
            work_files={"draft": "draft.txt"},
        )

    async def close(self):
        self.closed = True


class BlockingRewritePipeline(FakeRewritePipeline):
    def __init__(self, settings):
        super().__init__(settings)
        self.release = threading.Event()

    async def process(self, job, source_job, update):
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return await super().process(job, source_job, update)


def _create_source_job(
    settings,
    repository,
    *,
    job_id="source-1",
    status=JobStatus.COMPLETED,
    body="Original transcript content.",
):
    output_dir = settings.jobs_dir / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / "source.txt"
    txt_path.write_text(f"Title: Original title\n\n{body}\n", encoding="utf-8")
    job = repository.create_job(
        job_id=job_id,
        cache_key=f"source:{job_id}",
        request_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        requested_language=None,
        force_refresh=False,
    )
    return repository.update_job(
        job.id,
        status=status,
        progress=100 if status == JobStatus.COMPLETED else 0,
        source=TranscriptSource.MANUAL_CAPTION,
        actual_language="vi",
        video_json={
            "id": "dQw4w9WgXcQ",
            "title": "Original title",
            "channel": "Channel",
            "duration_seconds": 60,
            "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        },
        artifacts_json={"txt": str(txt_path)},
    )


def _wait_for_rewrite_status(client, job_id, expected):
    for _ in range(100):
        response = client.get(f"/api/v1/rewrite-jobs/{job_id}")
        if response.json()["status"] in expected:
            return response
        time.sleep(0.01)
    raise AssertionError(f"rewrite job did not reach {expected}")


def test_create_poll_cache_force_refresh_and_download(settings):
    repository = JobRepository(settings.database_path)
    repository.initialize()
    source_job = _create_source_job(settings, repository)
    rewrite_repository = RewriteJobRepository(settings.database_path)
    pipeline = FakeRewritePipeline(settings)
    app = create_app(
        settings,
        repository=repository,
        pipeline=IdleTranscriptPipeline(),
        rewrite_repository=rewrite_repository,
        rewrite_pipeline=pipeline,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/rewrite-jobs",
            json={"transcript_job_id": source_job.id},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

        completed = _wait_for_rewrite_status(client, job_id, {"completed"})
        payload = completed.json()
        assert payload["stage"] == "rendering"
        assert payload["title"] == "SEO title"
        assert payload["source_length"] == 100
        assert payload["output_length"] == 110
        assert payload["sections_completed"] == 2
        assert payload["sections_total"] == 2
        assert payload["video"]["id"] == "dQw4w9WgXcQ"
        assert payload["language"] == "vi"

        artifact = client.get(f"/api/v1/rewrite-jobs/{job_id}/artifacts/txt")
        assert artifact.status_code == 200
        assert artifact.text.startswith("Title: SEO title")

        cached = client.post(
            "/api/v1/rewrite-jobs",
            json={"transcript_job_id": source_job.id},
        )
        assert cached.status_code == 200
        assert cached.json()["id"] == job_id
        assert cached.json()["cached"] is True

        duplicate_source = _create_source_job(settings, repository, job_id="source-2")
        cached_duplicate_source = client.post(
            "/api/v1/rewrite-jobs",
            json={"transcript_job_id": duplicate_source.id},
        )
        assert cached_duplicate_source.status_code == 200
        assert cached_duplicate_source.json()["id"] == job_id

        refreshed = client.post(
            "/api/v1/rewrite-jobs",
            json={"transcript_job_id": source_job.id, "force_refresh": True},
        )
        assert refreshed.status_code == 202
        assert refreshed.json()["id"] != job_id

    assert pipeline.closed is True


def test_rewrite_rejects_missing_incomplete_and_empty_sources(settings):
    repository = JobRepository(settings.database_path)
    repository.initialize()
    _create_source_job(
        settings,
        repository,
        job_id="incomplete",
        status=JobStatus.QUEUED,
    )
    _create_source_job(settings, repository, job_id="empty", body="")
    app = create_app(
        settings,
        repository=repository,
        pipeline=IdleTranscriptPipeline(),
        rewrite_pipeline=FakeRewritePipeline(settings),
    )

    with TestClient(app) as client:
        missing = client.post("/api/v1/rewrite-jobs", json={"transcript_job_id": "missing"})
        incomplete = client.post("/api/v1/rewrite-jobs", json={"transcript_job_id": "incomplete"})
        empty = client.post("/api/v1/rewrite-jobs", json={"transcript_job_id": "empty"})
        missing_job = client.get("/api/v1/rewrite-jobs/missing")

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "SOURCE_NOT_FOUND"
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"]["code"] == "SOURCE_NOT_COMPLETED"
    assert empty.status_code == 422
    assert empty.json()["detail"]["code"] == "SOURCE_EMPTY"
    assert missing_job.status_code == 404
    assert missing_job.json()["detail"]["code"] == "REWRITE_JOB_NOT_FOUND"


def test_active_rewrite_is_reused_and_artifact_is_locked(settings):
    repository = JobRepository(settings.database_path)
    repository.initialize()
    source_job = _create_source_job(settings, repository)
    pipeline = BlockingRewritePipeline(settings)
    app = create_app(
        settings,
        repository=repository,
        pipeline=IdleTranscriptPipeline(),
        rewrite_pipeline=pipeline,
    )

    with TestClient(app) as client:
        try:
            created = client.post(
                "/api/v1/rewrite-jobs",
                json={"transcript_job_id": source_job.id},
            )
            job_id = created.json()["id"]
            artifact = client.get(f"/api/v1/rewrite-jobs/{job_id}/artifacts/txt")
            duplicate = client.post(
                "/api/v1/rewrite-jobs",
                json={"transcript_job_id": source_job.id},
            )

            assert artifact.status_code == 409
            assert artifact.json()["detail"]["code"] == "ARTIFACT_NOT_READY"
            assert duplicate.status_code == 202
            assert duplicate.json()["id"] == job_id
        finally:
            pipeline.release.set()

        _wait_for_rewrite_status(client, job_id, {"completed"})


def test_rewrite_worker_requeues_unfinished_job_with_checkpoint(settings):
    repository = JobRepository(settings.database_path)
    repository.initialize()
    source_job = _create_source_job(settings, repository)
    rewrite_repository = RewriteJobRepository(settings.database_path)
    rewrite_repository.initialize()
    rewrite_job = rewrite_repository.create_job(
        job_id="rewrite-restart",
        transcript_job_id=source_job.id,
        source_hash="hash",
        cache_key="restart-cache",
        force_refresh=False,
        source_language="vi",
    )
    rewrite_repository.update_job(
        rewrite_job.id,
        status=JobStatus.RUNNING,
        stage=RewriteStage.REWRITING,
        progress=50,
        sections_completed=1,
        sections_total=2,
        checkpoint_json={"next_section": 2},
    )
    pipeline = FakeRewritePipeline(settings)
    app = create_app(
        settings,
        repository=repository,
        pipeline=IdleTranscriptPipeline(),
        rewrite_repository=rewrite_repository,
        rewrite_pipeline=pipeline,
    )

    with TestClient(app) as client:
        completed = _wait_for_rewrite_status(client, rewrite_job.id, {"completed"})

    assert completed.json()["status"] == "completed"
    assert pipeline.calls == 1
    assert pipeline.seen_checkpoints == [{"next_section": 2}]


def test_rewrite_failure_exposes_typed_error(settings):
    repository = JobRepository(settings.database_path)
    repository.initialize()
    source_job = _create_source_job(settings, repository)
    pipeline = FakeRewritePipeline(
        settings,
        fail=PipelineError(
            "GPT_LOGIN_REQUIRED",
            "Manual ChatGPT login is required.",
        ),
    )
    app = create_app(
        settings,
        repository=repository,
        pipeline=IdleTranscriptPipeline(),
        rewrite_pipeline=pipeline,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/rewrite-jobs",
            json={"transcript_job_id": source_job.id},
        )
        failed = _wait_for_rewrite_status(client, created.json()["id"], {"failed"})

    assert failed.json()["error"] == {
        "code": "GPT_LOGIN_REQUIRED",
        "message": "Manual ChatGPT login is required.",
        "retryable": False,
        "details": {},
    }
